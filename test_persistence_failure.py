"""Verified captures survive ordinary PDF failures, never identity/cancel errors."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from acquisition_journal import persist_capture
from activity import Activities
from ledger import Store
from login_credentials import CredentialError
from sbi_views import _jst_now,init_observations,save_observation
from update_operations import apply_saved,collect_and_apply
from update_workflow import UpdateError


class PersistenceFailure(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        live=Store(self.base/'data');Activities(live);init_observations(live)
        live.set_owner('C','Anonymous C');live.close()
        self.stage=Store(self.base/'stage');self.stage.set_owner('C','Anonymous C')
        self.addCleanup(self.stage.close)
        self.page=Mock();self.page.url='https://m.sbisec.co.jp/account'
        self.page.is_closed.return_value=False;self.page.frames=[self.page]
        self.page.locator.return_value.inner_text.return_value='Anonymous C 様 ログアウト'
        self.pw=Mock();self.browser=self.pw.chromium.launch.return_value
        context=self.browser.new_context.return_value
        context.new_page.return_value=self.page;context.pages=[self.page]
        self.messages=[]
        self.journal=Mock(wraps=persist_capture)
        self.mocks={
            'auto_login.BASE':self.base,
            'auto_login.autofill_login':Mock(return_value=True),
            'auto_login._login_page':Mock(return_value=self.page),
            'auto_login.recent_ids':Mock(return_value=set()),
            'auto_login.submit_login':Mock(),
            'auto_login.visible':Mock(return_value=[]),
            'auto_login.otp_fields':Mock(return_value=[]),
            'auto_login.authenticated_page':Mock(return_value=self.page),
            'sbi_views.landing':Mock(return_value=self.page),
            'auto_login.open_electronic_post':Mock(return_value=self.page),
            'sbi_views.verify_post_owner':Mock(),
            'auto_login.collect':Mock(return_value=(1,0,0)),
            'sbi_views.capture_account':Mock(side_effect=self.capture),
            'acquisition_journal.persist_capture':self.journal,
        }

    def capture(self,page,store,account,progress,**kwargs):
        save_observation(store,account,'口座サマリー','',{'cash_total':123})
        return False # Partial data is still valid evidence, with explicit gaps.

    def execute(self,cancelled=lambda:False,capture_assets=True):
        from auto_login import run_one
        with ExitStack() as stack:
            for name,value in self.mocks.items():stack.enter_context(patch(name,value))
            return run_one(self.pw,'C',self.stage,gmail_session=(Mock(),'anonymous@example.test'),
                progress=self.messages.append,cancelled=cancelled,capture_assets=capture_assets)

    def no_live_assets(self):
        live=Store(self.base/'data')
        try:self.assertEqual(live.db.execute('SELECT count(*) FROM account_observations').fetchone()[0],0)
        finally:live.close()

    def test_disk_full_is_blocking_and_never_claims_durable_capture(self):
        self.journal.side_effect=OSError('simulated disk full')
        with self.assertRaisesRegex(CredentialError,'証跡を保存できません'):self.execute()
        self.journal.assert_called_once();self.browser.close.assert_called_once()
        self.assertFalse((self.base/'data/acquisitions').exists())
        self.assertFalse(any('取得証跡を保存しました' in text for text in self.messages))
        self.no_live_assets()

    def test_post_owner_mismatch_never_attempts_assets_or_journal(self):
        self.mocks['sbi_views.verify_post_owner'].side_effect=CredentialError('別口座の名義')
        with self.assertRaisesRegex(CredentialError,'別口座'):self.execute()
        self.mocks['auto_login.collect'].assert_not_called()
        self.mocks['sbi_views.capture_account'].assert_not_called();self.journal.assert_not_called()
        self.browser.close.assert_called_once();self.no_live_assets()

    def test_asset_identity_failure_after_pdf_failure_never_persists_partial_success(self):
        self.mocks['auto_login.collect'].return_value=(1,0,1)
        self.mocks['sbi_views.capture_account'].side_effect=CredentialError('資産画面に別口座の名義')
        with self.assertRaisesRegex(CredentialError,'別口座'):self.execute()
        self.journal.assert_not_called();self.browser.close.assert_called_once();self.no_live_assets()

    def test_conflicting_confirmed_history_is_blocking_and_never_journaled(self):
        from asset_snapshots import HistoricalConflict
        self.mocks['sbi_views.capture_account'].side_effect=HistoricalConflict('同じ基準日の確認済み資産と取得値が一致しません。')
        with self.assertRaisesRegex(CredentialError,'確認済み記録'):
            self.execute()
        self.journal.assert_not_called();self.browser.close.assert_called_once();self.no_live_assets()

    def test_cancel_during_pdf_never_continues_to_assets_or_journal(self):
        stopped=[False]
        def stop(*args,**kwargs):stopped[0]=True;raise CredentialError('中止しました。')
        self.mocks['auto_login.collect'].side_effect=stop
        with self.assertRaisesRegex(CredentialError,'中止'):self.execute(lambda:stopped[0])
        self.mocks['sbi_views.capture_account'].assert_not_called();self.journal.assert_not_called()
        self.browser.close.assert_called_once();self.no_live_assets()

    def test_cancel_after_capture_before_persistence_keeps_it_out_of_journal(self):
        stopped=[False]
        def stop(*args,**kwargs):
            answer=self.capture(*args,**kwargs);stopped[0]=True;return answer
        self.mocks['sbi_views.capture_account'].side_effect=stop
        with self.assertRaisesRegex(CredentialError,'中止'):self.execute(lambda:stopped[0])
        self.journal.assert_not_called();self.browser.close.assert_called_once();self.no_live_assets()

    def test_ordinary_pdf_transport_failure_keeps_verified_assets_but_is_not_success(self):
        self.mocks['auto_login.collect'].side_effect=RuntimeError('simulated transport failure')
        with self.assertRaisesRegex(CredentialError,'取得証跡は保持'):self.execute()
        self.mocks['sbi_views.capture_account'].assert_called_once();self.journal.assert_called_once()
        self.assertEqual(len(list((self.base/'data/acquisitions/C').glob('*.json'))),1)
        self.browser.close.assert_called_once();self.no_live_assets()

    def test_pdf_failure_with_no_asset_evidence_does_not_claim_assets_retained(self):
        self.mocks['auto_login.collect'].return_value=(1,0,1)
        self.mocks['sbi_views.capture_account'].side_effect=None
        self.mocks['sbi_views.capture_account'].return_value=False
        with self.assertRaises(CredentialError) as error:self.execute()
        self.assertNotIn('資産の取得証跡は保持',str(error.exception))
        self.assertFalse((self.base/'data/acquisitions').exists())
        self.browser.close.assert_called_once();self.no_live_assets()

    def test_navigation_failure_requires_independent_owner_before_retaining_assets(self):
        self.mocks['auto_login.open_electronic_post'].side_effect=CredentialError('電子ポストの経路なし')
        with self.assertRaisesRegex(CredentialError,'電子ポストへの自動移動'):self.execute()
        self.mocks['auto_login.authenticated_page'].assert_called_once()
        self.mocks['sbi_views.verify_post_owner'].assert_not_called()
        self.mocks['auto_login.collect'].assert_not_called()
        self.mocks['sbi_views.capture_account'].assert_called_once()
        self.assertEqual(len(list((self.base/'data/acquisitions/C').glob('*.json'))),1)
        self.browser.close.assert_called_once();self.no_live_assets()

    def test_navigation_failure_without_matching_sbi_owner_never_captures(self):
        self.mocks['auto_login.open_electronic_post'].side_effect=CredentialError('電子ポストの経路なし')
        self.mocks['auto_login.authenticated_page'].side_effect=CredentialError('別口座のログイン名義')
        with self.assertRaisesRegex(CredentialError,'別口座'):self.execute()
        self.mocks['sbi_views.capture_account'].assert_not_called();self.journal.assert_not_called()
        self.mocks['auto_login.collect'].assert_not_called()
        self.browser.close.assert_called_once();self.no_live_assets()

    def test_individual_pdf_only_never_enters_asset_fallback(self):
        self.mocks['auto_login.open_electronic_post'].side_effect=CredentialError('電子ポストの経路なし')
        with self.assertRaisesRegex(CredentialError,'電子ポストの経路なし'):self.execute(capture_assets=False)
        self.mocks['auto_login.authenticated_page'].assert_not_called()
        self.mocks['sbi_views.capture_account'].assert_not_called();self.journal.assert_not_called()
        self.browser.close.assert_called_once();self.no_live_assets()


class BatchRecovery(unittest.TestCase):
    def test_later_account_failure_discards_stage_but_saved_apply_recovers_four_captures(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);live=Store(base/'data');Activities(live);init_observations(live)
            for account in 'ABCD':live.set_owner(account,'Anonymous '+account)
            live.close();calls=[]
            def acquire(pw,account,stage,**kwargs):
                calls.append(account);since=_jst_now().isoformat()
                save_observation(stage,account,'口座サマリー','',{'cash_total':123})
                persist_capture(base,account,'Anonymous '+account,stage,since,target_dates=kwargs['target_dates'])
                if account=='B':raise CredentialError('PDF取得の一部失敗。検証済み資産は証跡に保持。')
                return {'complete':True,'assets_observed':False,'assets_missing':['domestic']}
            with self.assertRaises(UpdateError):
                collect_and_apply(base,'2026-10',Mock(),Mock(return_value=(Mock(),'anonymous@example.test')),
                    acquire,lambda *args:self.fail('failed collection must not commit'))
            self.assertEqual(calls,list('ABCD'))
            live=Store(base/'data')
            self.assertEqual(live.db.execute('SELECT count(*) FROM account_observations').fetchone()[0],0)
            live.close();self.assertFalse((base/'output').exists())
            self.assertEqual(len(list((base/'data/acquisitions').glob('*/*.json'))),4)
            result=apply_saved(base,'2026-10',lambda *args:True)
            self.assertEqual(result['summary']['recovered_asset_accounts'],4)
            live=Store(base/'data')
            self.assertEqual(live.db.execute('SELECT count(*) FROM account_observations').fetchone()[0],4)
            self.assertEqual(live.records(),[]);live.close()


if __name__=='__main__':unittest.main()
