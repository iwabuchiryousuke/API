"""Account acquisition must leave e-post and reselect the verified SBI session.

The navigation boundary is doubled; real local-browser selection tests live in
the asset navigation suite. These tests mutate Page.url as a real same-tab
redirect would, which older login mocks failed to exercise.
"""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from acquisition_journal import persist_capture
from ledger import Store
from login_credentials import CredentialError
from sbi_views import save_observation


OWNER='Anonymous C'
ENTRY='https://m.sbisec.co.jp/account-summary?session=fixture-session-marker'
POST='https://plus.e-shishobako.ne.jp/dp_apl/usr/#/user-delivery'


def page(url):
    value=Mock();value.url=url;value.frames=[value];value.is_closed.return_value=False
    value.locator.return_value.inner_text.return_value=OWNER+' 様 ログアウト 口座管理'
    value.locator.return_value.count.return_value=0
    return value


class AssetRecoveryIntegration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.stage=Store(self.base/'stage')
        self.stage.set_owner('C',OWNER);self.addCleanup(self.stage.close)
        self.main=page(ENTRY);self.asset=page(ENTRY)
        self.pw=Mock();self.browser=self.pw.chromium.launch.return_value
        self.context=self.browser.new_context.return_value
        self.context.new_page.return_value=self.main;self.context.pages=[self.main]
        self.main.context=self.context;self.asset.context=self.context
        self.messages=[];self.events=[]
        self.selector=Mock(side_effect=self.restore)
        self.capture=Mock(side_effect=self.capture_assets)
        self.journal=Mock(wraps=persist_capture)
        self.mocks={
            'auto_login.BASE':self.base,
            'auto_login.autofill_login':Mock(return_value=True),
            'auto_login._login_page':Mock(return_value=self.main),
            'auto_login.recent_ids':Mock(return_value=set()),
            'auto_login.submit_login':Mock(),
            'auto_login.visible':Mock(return_value=[]),
            'auto_login.otp_fields':Mock(return_value=[]),
            'sbi_views.landing':Mock(return_value=self.main),
            'auto_login.open_electronic_post':Mock(side_effect=self.same_tab_post),
            'sbi_views.verify_post_owner':Mock(),
            'auto_login.collect':Mock(return_value=(1,0,0)),
            'sbi_views.select_asset_page':self.selector,
            'sbi_views.capture_account':self.capture,
            'acquisition_journal.persist_capture':self.journal,
        }

    def same_tab_post(self,*args,**kwargs):
        self.main.url=POST
        self.events.append('post')
        return self.main

    def restore(self,original,**kwargs):
        self.assertEqual(original.url,POST)
        self.assertEqual(kwargs.get('entry_url'),ENTRY)
        self.assertEqual(kwargs.get('owner'),OWNER)
        self.events.append('restore-sbi')
        return self.asset

    def capture_assets(self,selected,store,account,progress,**kwargs):
        from auto_login import trusted
        self.assertIs(selected,self.asset)
        self.assertTrue(trusted(selected))
        self.assertEqual(account,'C');self.assertEqual(kwargs.get('owner'),OWNER)
        self.events.append('capture')
        save_observation(store,account,'口座サマリー','',{'cash_total':123})
        return False # A verified partial current capture, without invented zeros.

    def execute(self,cancelled=lambda:False,capture_assets=True):
        from auto_login import run_one
        with ExitStack() as stack:
            for name,value in self.mocks.items():stack.enter_context(patch(name,value,create=True))
            return run_one(self.pw,'C',self.stage,gmail_session=(Mock(),'anonymous@example.test'),
                progress=self.messages.append,cancelled=cancelled,capture_assets=capture_assets)

    def test_same_tab_post_redirect_restores_observed_home_before_capture(self):
        result=self.execute()
        self.assertEqual(self.events,['post','restore-sbi','capture'])
        self.selector.assert_called_once();self.capture.assert_called_once()
        self.assertTrue(result['complete']);self.assertEqual(result['saved'],1)
        self.assertEqual(len(list((self.base/'data/acquisitions/C').glob('*.json'))),1)
        self.assertNotIn('fixture-session-marker','\n'.join(self.messages))
        self.browser.close.assert_called_once()

    def test_popup_post_reuses_retained_sbi_page(self):
        post=page(POST)
        post.context=self.context
        def popup(*args,**kwargs):
            self.context.pages=[self.main,post];self.events.append('post-popup');return post
        def reuse(original,**kwargs):
            self.assertIs(original,self.main);self.assertEqual(original.url,ENTRY)
            self.assertEqual(kwargs.get('entry_url'),ENTRY)
            self.asset=self.main;self.events.append('reuse-sbi');return self.main
        self.mocks['auto_login.open_electronic_post'].side_effect=popup
        self.selector.side_effect=reuse
        result=self.execute()
        self.assertTrue(result['complete'])
        self.assertEqual(self.events,['post-popup','reuse-sbi','capture'])
        self.journal.assert_called_once();self.browser.close.assert_called_once()

    def test_wrong_owner_on_restoration_never_captures_or_journals(self):
        self.selector.side_effect=CredentialError('別口座の名義を検出しました。')
        with self.assertRaisesRegex(CredentialError,'別口座'):self.execute()
        self.capture.assert_not_called();self.journal.assert_not_called()
        self.assertFalse((self.base/'data/acquisitions').exists())
        self.browser.close.assert_called_once()

    def test_cancel_while_recovering_sbi_stops_before_capture(self):
        stopped=[False]
        def stop(*args,**kwargs):stopped[0]=True;raise CredentialError('中止しました。')
        self.selector.side_effect=stop
        with self.assertRaisesRegex(CredentialError,'中止'):self.execute(lambda:stopped[0])
        self.capture.assert_not_called();self.journal.assert_not_called()
        self.browser.close.assert_called_once()

    def test_untrusted_recorded_entry_is_refused_without_capture_or_url_logging(self):
        unsafe='https://example.invalid/account?session=fixture-private-marker'
        def landing(*args,**kwargs):self.main.url=unsafe;return self.main
        self.mocks['sbi_views.landing'].side_effect=landing
        self.selector.side_effect=CredentialError('SBIの安全な復帰先を確認できません。')
        with self.assertRaises(CredentialError):self.execute()
        self.capture.assert_not_called();self.journal.assert_not_called()
        self.assertNotIn('fixture-private-marker','\n'.join(self.messages))
        # The only goto here is the normal SBI initial page, never the bad URL.
        self.assertTrue(all(call.args[0]!='https://example.invalid/account?session=fixture-private-marker'
            for call in self.main.goto.call_args_list))
        self.browser.close.assert_called_once()

    def test_individual_pdf_only_does_not_restore_or_capture_assets(self):
        result=self.execute(capture_assets=False)
        self.assertTrue(result['complete']);self.assertEqual(result['saved'],1)
        self.selector.assert_not_called();self.capture.assert_not_called();self.journal.assert_not_called()
        self.browser.close.assert_called_once()


if __name__=='__main__':unittest.main()
