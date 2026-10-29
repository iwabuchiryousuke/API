from contextlib import ExitStack
from datetime import date,datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from activity import Activities
from acquisition_targets import target_dates
from asset_snapshots import merge_exact
from ledger import LedgerError,Store,Snapshot
from login_credentials import CredentialError
from sbi_views import _jst_now,save_observation
from update_dialog import warning_lines
from update_operations import collect_and_apply,acquire_selected_pdf,apply_saved


class OneButtonUpdate(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        store=Store(self.base/'data');Activities(store)
        for account in 'ABCDE':store.set_owner(account,'Owner '+account)
        store.close()

    def add_pending_pdfs(self):
        for account in 'ABCDE':
            folder=self.base/'data'/'電子ポストPDF'/account;folder.mkdir(parents=True)
            (folder/'pending.pdf').write_bytes(b'fixture-original-'+account.encode())

    def capture(self,pw,account,stage,**kwargs):
        self.assertTrue(kwargs['documents']);self.assertTrue(kwargs['capture_assets'])
        self.assertTrue(kwargs['current_only']);self.assertEqual(kwargs['target_dates'],())
        self.assertIsNotNone(kwargs['gmail_session'])
        save_observation(stage,account,'口座サマリー','',dict(cash_total=350,growth_remaining={'year':2026,'remaining':500},tsumitate_remaining={'year':2026,'remaining':600}))
        save_observation(stage,account,'口座(外貨建)','',dict(usd_cash=10))
        return dict(complete=True,assets_observed=True,assets_missing=[],saved=1,duplicate=0)

    def test_one_button_acquires_four_and_applies_all_five_with_current_excel_contract(self):
        self.add_pending_pdfs();acquired=[];reviews=[]
        store=Store(self.base/'data')
        trade=dict(trade_date='2026-10-02',settlement_date='2026-10-06',name='Example Security',code='9900',market='東証',side='買',
                   custody='特定',tax_class='',quantity='10',price='100',fees='0',tax='0',settlement_amount='1000')
        raw=json.dumps([trade]).encode()
        store.import_snapshot('A',Snapshot(raw,'example.csv',hashlib.sha256(raw).hexdigest(),'2026-10-02','2026-10-02',[trade]))
        merge_exact(store,'A','2026-09-30',dict(cash=999,growth_remaining=123))
        before=store.records()
        old=store.db.execute("SELECT payload FROM asset_snapshots WHERE account='A' AND as_of='2026-09-30'").fetchone()[0]
        store.close()
        def capture(pw,account,stage,**kwargs):
            acquired.append(account);return self.capture(pw,account,stage,**kwargs)
        def review(summary,proposal,prepared):
            reviews.append(summary)
            self.assertEqual(summary['excluded_pdf_files'],5)
            self.assertEqual({row['account'] for row in proposal if row['kind']=='対象外'},set('ABCDE'))
            self.assertFalse(any(w.startswith('ASSET_DATE_INCOMPLETE:') for w in summary['warnings']))
            self.assertTrue(any(w.startswith('PDF_NOT_PARSED:') for w in summary['warnings']))
            self.assertEqual(summary['daily_asset_records'],0)
            return True
        auth=Mock(return_value=(Mock(),'recipient@example.test'))
        captured_at=datetime(2026,10,29,16,15,tzinfo=timezone(timedelta(hours=9)))
        with patch('update_workflow.classify',side_effect=LedgerError('対象外：外国株式の通知（未解析）。原本保持。')) as classify, \
             patch('sbi_views._jst_now',return_value=captured_at):
            result=collect_and_apply(self.base,'2026-10',Mock(),auth,capture,review,current_only=True)
        self.assertEqual(acquired,list('ABCD'));self.assertEqual(auth.call_count,4)
        self.assertEqual(classify.call_count,5);self.assertEqual(len(reviews),1)
        self.assertEqual(result['summary']['assets_observed'],4)
        with ZipFile(result['output']) as z:
            root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        cells={cell.attrib['r']:cell for cell in root.findall('.//s:c',ns)}
        for ref in ('K2','L2','M2'):self.assertIsNotNone(cells[ref].find('s:f',ns))
        self.assertEqual(cells['O1'].findtext('s:v',namespaces=ns),str((captured_at.date()-date(1899,12,30)).days))
        for ref,value in (('P2',500),('Q2',600),('R2',350),('S2',10)):
            self.assertEqual(float(cells[ref].findtext('s:v',namespaces=ns)),value)
        self.assertNotIn('M12',cells)
        store=Store(self.base/'data')
        try:
            self.assertEqual(store.records(),before)
            self.assertEqual(store.db.execute("SELECT payload FROM asset_snapshots WHERE account='A' AND as_of='2026-09-30'").fetchone()[0],old)
        finally:store.close()

    def test_selected_pdf_action_never_applies_a_ledger(self):
        auth=Mock(return_value=(Mock(),'recipient@example.test'))
        acquire=Mock(return_value={'saved':2,'duplicate':3,'complete':True})
        before=(self.base/'data/ledger.sqlite3').read_bytes()
        with patch('update_operations.Workflow',side_effect=AssertionError('no apply during acquisition')):
            result=acquire_selected_pdf(self.base,'C',Mock(),auth,acquire)
        self.assertFalse(result['applied'])
        self.assertFalse(acquire.call_args.kwargs['capture_assets'])
        self.assertEqual((self.base/'data/ledger.sqlite3').read_bytes(),before)

    def test_saved_action_inspects_all_accounts_and_warns_before_review(self):
        self.add_pending_pdfs();review=Mock(return_value=True)
        with patch('update_workflow.classify',side_effect=LedgerError('対象外：外国株式等株式分割・権利売却等のご案内（未解析）')) as classify:
            result=apply_saved(self.base,'2026-10',review)
        self.assertEqual(classify.call_count,5)
        summary=review.call_args.args[0]
        self.assertEqual(summary['excluded_pdf_files'],5)
        self.assertTrue(any('未解析PDF 5件' in line for line in warning_lines(summary['warnings'])))
        self.assertTrue(result['output'].exists())

    def test_calendar_targets_include_leap_year_and_jst_default(self):
        self.assertEqual(target_dates('2024-03'),('2024-02-29','2024-03-01'))
        self.assertEqual(target_dates('2026-01'),('2025-12-31','2026-01-01'))
        self.assertEqual(target_dates('',today=date(2026,10,22)),('2026-09-30','2026-10-01'))


class InterruptedAcquisition(unittest.TestCase):
    def test_pdf_failure_still_collects_assets_and_all_account_apply_recovers_them(self):
        from auto_login import run_one
        with tempfile.TemporaryDirectory() as folder,tempfile.TemporaryDirectory() as staged:
            base=Path(folder);live=Store(base/'data');Activities(live);live.set_owner('C','Owner C');live.close()
            stage=Store(staged);stage.set_owner('C','Owner C')
            page=Mock();page.url='https://m.sbisec.co.jp/account';page.is_closed.return_value=False;page.frames=[page]
            page.locator.return_value.inner_text.return_value='Owner C 様 ログアウト 口座管理'
            page.locator.return_value.count.return_value=0
            pw=Mock();browser=pw.chromium.launch.return_value;context=browser.new_context.return_value
            context.new_page.return_value=page;context.pages=[page]
            page.context=context
            events=[]
            def capture(p,store,account,progress,**kwargs):
                events.append('assets')
                save_observation(store,account,'口座サマリー','',dict(cash_total=350,growth_remaining={'year':_jst_now().year,'remaining':500},tsumitate_remaining={'year':_jst_now().year,'remaining':600}))
                save_observation(store,account,'My資産','',dict(domestic=100,foreign=200,cash=300,bank=50,foreign_cash=400,usd_cash=10))
                return True
            mocks={
                'auto_login.BASE':base,'auto_login.autofill_login':Mock(return_value=True),
                'auto_login._login_page':Mock(return_value=page),'auto_login.recent_ids':Mock(return_value=set()),
                'auto_login.submit_login':Mock(),'auto_login.visible':Mock(return_value=[]),'auto_login.otp_fields':Mock(return_value=[]),
                'sbi_views.landing':Mock(return_value=page),'auto_login.open_electronic_post':Mock(return_value=page),
                'sbi_views.verify_post_owner':Mock(),'auto_login.collect':Mock(return_value=(1,0,1)),
                'sbi_views.capture_account':capture,
            }
            with ExitStack() as stack:
                for name,value in mocks.items():stack.enter_context(patch(name,value))
                with self.assertRaisesRegex(CredentialError,'取得証跡は保持'):
                    run_one(pw,'C',stage,gmail_session=(Mock(),'recipient@example.test'))
            self.assertEqual(events,['assets']);stage.close();browser.close.assert_called_once()
            self.assertEqual(len(list((base/'data/acquisitions/C').glob('*.json'))),1)
            result=apply_saved(base,'2026-10',lambda *args:True)
            self.assertEqual(result['summary']['recovered_asset_accounts'],1)
            store=Store(base/'data')
            count=store.db.execute("SELECT count(*) FROM account_observations WHERE account='C'").fetchone()[0]
            self.assertEqual(count,2);self.assertEqual(len(store.records()),0);store.close()


if __name__=='__main__':unittest.main()
