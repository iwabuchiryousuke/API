"""Asset-only acquisition must stay isolated from PDF imports and stale data."""
from contextlib import ExitStack
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from activity import Activities
from asset_snapshots import init as init_assets
from ledger import Snapshot,Store
from login_credentials import CredentialError
from sbi_views import capture_status,init_observations,save_observation
from update_workflow import Workflow,UpdateError


JST=timezone(timedelta(hours=9))
NOW=datetime(2026,10,22,12,0,tzinfo=JST)


class WorkflowModes(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        store=Store(self.base/'data');Activities(store);init_assets(store);init_observations(store)
        store.rename('C','Existing custom label');store.set_owner('C','Existing custom identity')
        trade=dict(trade_date='2026-01-01',settlement_date='2026-01-05',name='匿名試験株',
            code='1234',market='東証',side='買',custody='特定',tax_class='',quantity='1',
            price='100',fees='0',tax='0',settlement_amount='100')
        raw=json.dumps(trade).encode()
        store.import_snapshot('C',Snapshot(raw,'anonymous.csv',hashlib.sha256(raw).hexdigest(),
            '2026-01-01','2026-01-01',[trade]))
        self.baseline={table:[tuple(row) for row in store.db.execute('SELECT * FROM '+table)]
            for table in ('accounts','imports','trades','evidence','activities','activity_files',
                'balance_reports','asset_snapshots','account_observations')}
        store.close()
        self.bad=self.base/'data/電子ポストPDF/C/broken.pdf';self.bad.parent.mkdir(parents=True)
        self.bad.write_bytes(b'not a PDF - must remain untouched')

    def tearDown(self):self.tmp.cleanup()

    def collect(self,account,stage):
        self.assertEqual(account,'C')
        save_observation(stage,account,'口座サマリー','',{'cash_total':123})
        return {'complete':True,'assets_observed':False,'assets_missing':['foreign']}

    def read_tables(self):
        store=Store(self.base/'data')
        try:return {table:[tuple(row) for row in store.db.execute('SELECT * FROM '+table)]
            for table in self.baseline}
        finally:store.close()

    def test_asset_only_skips_bad_pdf_and_commits_only_new_observation(self):
        reviews=[]
        result=Workflow(self.base).run('2026-10',collect=self.collect,accounts=('C',),
            include_saved_pdfs=False,review=lambda *args:reviews.append(args) or True)
        after=self.read_tables()
        for table in self.baseline:
            if table!='account_observations':self.assertEqual(after[table],self.baseline[table],table)
        self.assertEqual(len(after['account_observations']),1)
        self.assertEqual(result['summary']['new_trades'],0)
        self.assertEqual(result['summary']['new_pdf_files'],0)
        self.assertEqual(reviews[0][1],[])
        self.assertTrue(result['output'].is_file())
        self.assertEqual(self.bad.read_bytes(),b'not a PDF - must remain untouched')

    def test_normal_workflow_rejects_bad_pdf_and_rolls_back_observation(self):
        review=Mock(return_value=True)
        with self.assertRaises(UpdateError) as error:
            Workflow(self.base).run('2026-10',collect=self.collect,accounts=('C',),review=review)
        self.assertEqual(error.exception.code,'UNSUPPORTED_PDF')
        self.assertEqual(self.read_tables(),self.baseline)
        review.assert_not_called()
        self.assertFalse((self.base/'output').exists())


class AcquisitionFreshness(unittest.TestCase):
    def test_old_same_day_fields_are_not_reported_as_newly_acquired(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder)
            old=NOW-timedelta(hours=1)
            with patch('sbi_views._jst_now',return_value=old):
                save_observation(store,'C','口座サマリー','',{'cash_total':123,
                    'growth_remaining':{'year':2026,'remaining':100},
                    'tsumitate_remaining':{'year':2026,'remaining':200}})
                save_observation(store,'C','My資産','',{'domestic':10,'foreign':20,
                    'cash':30,'bank':40,'foreign_cash':50,'usd_cash':1})
            with patch('sbi_views._jst_now',return_value=NOW):
                old_status=capture_status(store,'C')
                fresh=capture_status(store,'C',since=NOW.isoformat())
                self.assertTrue(old_status['available'])
                self.assertEqual(fresh['available'],[])
                self.assertFalse(fresh['usd_cash_observed'])
                save_observation(store,'C','口座サマリー','',{'cash_total':456})
                current=capture_status(store,'C',since=NOW.isoformat())
                self.assertEqual(current['available'],['cash'])
                self.assertIn('domestic',current['missing'])
                self.assertIn('growth_remaining',current['missing'])
                self.assertFalse(current['usd_cash_observed'])
            self.assertEqual(store.db.execute('SELECT count(*) FROM account_observations').fetchone()[0],3)
            store.close()

    def test_asset_only_run_fails_when_only_previous_observation_exists(self):
        from auto_login import run_one
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);store.set_owner('C','Custom C')
            with patch('sbi_views._jst_now',return_value=NOW-timedelta(hours=1)):
                save_observation(store,'C','口座サマリー','',{'cash_total':123})
            page=Mock();page.url='https://m.sbisec.co.jp/account';page.is_closed.return_value=False
            page.frames=[page];page.locator.return_value.inner_text.return_value='Custom C 様 ログアウト'
            playwright=Mock();browser=playwright.chromium.launch.return_value
            context=browser.new_context.return_value;context.pages=[page];context.new_page.return_value=page
            mocks={
                'auto_login.autofill_login':Mock(return_value=True),
                'auto_login._login_page':Mock(return_value=page),
                'auto_login.recent_ids':Mock(return_value=set()),
                'auto_login.submit_login':Mock(),
                'auto_login.visible':Mock(return_value=[]),
                'auto_login.otp_fields':Mock(return_value=[]),
                'sbi_views.landing':Mock(return_value=page),
                'auto_login.authenticated_page':Mock(return_value=page),
                'auto_login.open_electronic_post':Mock(),
                'auto_login.collect':Mock(),
                'sbi_views.capture_account':Mock(return_value=False),
            }
            with ExitStack() as stack:
                for name,value in mocks.items():stack.enter_context(patch(name,value))
                stack.enter_context(patch('sbi_views._jst_now',return_value=NOW))
                status=stack.enter_context(patch('sbi_views.capture_status',wraps=capture_status))
                with self.assertRaisesRegex(CredentialError,'資産情報を確認できません'):
                    run_one(playwright,'C',store,gmail_session=(Mock(),'anonymous@example.test'),documents=False)
                self.assertEqual(status.call_args.kwargs['since'],NOW.isoformat())
            browser.close.assert_called_once()
            mocks['auto_login.collect'].assert_not_called()
            mocks['auto_login.open_electronic_post'].assert_not_called()
            self.assertEqual(store.db.execute('SELECT count(*) FROM account_observations').fetchone()[0],1)
            store.close()


if __name__=='__main__':unittest.main()
