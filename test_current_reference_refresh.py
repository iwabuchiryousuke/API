"""Public price refresh is staged, optional, and cannot undo a financial commit."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from activity import Activities
from asset_snapshots import init as init_assets
from current_capture import load_current_capture
from ledger import LedgerError,Store
from sbi_views import init_observations,save_observation
from standalone_excel import export_trades as real_export
from update_operations import collect_and_apply
from update_workflow import Cancelled

JST=timezone(timedelta(hours=9))
NOW=datetime(2026,10,29,10,5,tzinfo=JST)


class CurrentReferenceRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);store=Store(self.base/'data');Activities(store);init_assets(store);init_observations(store)
        for account in 'ABCDE':store.set_owner(account,'Example '+account)
        store.close();self.order=[]
        self.market=self.base/'data/market';self.market.mkdir()
        self.old=dict(year=2026,securities={'0001':{'close':111,'close_date':'2026-09-30','manual_tag':'keep'}})
        self.source=self.market/'research_2026.json';self.write(self.source,self.old)
        self.original=self.source.read_bytes()
        self.notes=self.market/'user_notes.txt';self.notes.write_bytes(b'keep unrelated file')
        self.private=self.base/'data/private/opaque.enc';self.private.parent.mkdir();self.private.write_bytes(b'keep encrypted setting')
        self.new=dict(year=2026,securities={'0001':{'close':112,'close_date':'2026-09-30','month_start_close':120,'month_start_close_date':'2026-10-01','manual_tag':'keep'}})

    def write(self,path,value):path.write_text(json.dumps(value),encoding='utf-8')

    def acquire(self,pw,account,stage,**options):
        self.order.append('acquire-'+account)
        self.assertTrue(options['current_only']);self.assertEqual(options['target_dates'],())
        save_observation(stage,account,'口座サマリー','',dict(cash_total=100,growth_remaining={'year':2026,'remaining':200},tsumitate_remaining={'year':2026,'remaining':300}))
        save_observation(stage,account,'口座(外貨建)','',dict(usd_cash=12.5))
        return dict(complete=True,assets_observed=True,assets_missing=[],saved=0,duplicate=0)

    def refresh(self,stage,month):
        self.order.append('reference')
        self.assertEqual(month,'2026-10')
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertFalse((stage.directory/'market/user_notes.txt').exists())
        self.write(stage.directory/'market/research_2026.json',self.new)
        return dict(updated=['0001'],failed=[])

    def run_current(self,refresh=None,review=lambda *args:True):
        with patch('sbi_views._jst_now',return_value=NOW):
            return collect_and_apply(self.base,'2026-10',Mock(),Mock(return_value=(Mock(),'example@example.test')),
                                     self.acquire,review,current_only=True,reference_refresh=refresh)

    def assert_financial_committed(self):
        store=Store(self.base/'data')
        try:
            self.assertIsNotNone(load_current_capture(store))
            self.assertEqual(store.db.execute('SELECT count(*) FROM account_observations').fetchone()[0],8)
        finally:store.close()

    def test_hook_runs_after_pdf_staging_before_export_and_only_publishes_research(self):
        folder=self.base/'data/電子ポストPDF/A';folder.mkdir(parents=True)
        (folder/'notice.pdf').write_bytes(b'synthetic raw archive')
        def classify(path):
            self.order.append('pdf')
            raise LedgerError('対象外：売買明細ではない通知。')
        def export(store,*args):
            self.order.append('export')
            self.assertEqual(json.loads((store.directory/'market/research_2026.json').read_text()),self.new)
            self.assertEqual(self.source.read_bytes(),self.original)
            return real_export(store,*args)
        def review(*args):
            self.order.append('review');self.assertEqual(self.source.read_bytes(),self.original);return True
        with patch('update_workflow.classify',side_effect=classify),patch('update_workflow.export_trades',side_effect=export):
            result=self.run_current(self.refresh,review)
        self.assertEqual(self.order,[*('acquire-'+a for a in 'ABCD'),'pdf','reference','export','review'])
        self.assertEqual(json.loads(self.source.read_text()),self.new)
        self.assertEqual(self.notes.read_bytes(),b'keep unrelated file')
        self.assertEqual(self.private.read_bytes(),b'keep encrypted setting')
        self.assertFalse(any(w.startswith('REFERENCE_') for w in result['warnings']))
        self.assert_financial_committed()

    def test_cancel_does_not_publish_prices_or_current_financial_batch(self):
        with self.assertRaises(Cancelled):self.run_current(self.refresh,review=lambda *args:False)
        self.assertEqual(self.source.read_bytes(),self.original)
        store=Store(self.base/'data')
        try:
            self.assertIsNone(load_current_capture(store))
            self.assertEqual(store.db.execute('SELECT count(*) FROM account_observations').fetchone()[0],0)
        finally:store.close()

    def test_callback_error_restores_staged_cache_and_does_not_block_registration(self):
        def broken(stage,month):
            self.refresh(stage,month)
            self.write(stage.directory/'market/research_2027.json',dict(year=2027,securities={}))
            raise OSError('Synthetic network problem must not enter logs')
        def export(store,*args):
            self.assertEqual((store.directory/'market/research_2026.json').read_bytes(),self.original)
            self.assertFalse((store.directory/'market/research_2027.json').exists())
            return real_export(store,*args)
        with patch('update_workflow.export_trades',side_effect=export):result=self.run_current(broken)
        self.assertIn('REFERENCE_PRICES_UNAVAILABLE',result['warnings'])
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assert_financial_committed()

    def test_partial_fetch_success_keeps_verified_prices_with_warning(self):
        def partial(stage,month):
            self.refresh(stage,month);return dict(updated=['0001'],failed=['0002'])
        result=self.run_current(partial)
        self.assertIn('REFERENCE_PRICES_UNAVAILABLE',result['warnings'])
        self.assertEqual(json.loads(self.source.read_text()),self.new)
        self.assert_financial_committed()

    def test_concurrently_changed_live_public_cache_is_not_overwritten(self):
        concurrent=dict(year=2026,securities={'0001':{'manual_tag':'concurrent edit'}})
        def review(*args):self.write(self.source,concurrent);return True
        result=self.run_current(self.refresh,review)
        self.assertIn('REFERENCE_CACHE_CONFLICT',result['warnings'])
        self.assertEqual(json.loads(self.source.read_text()),concurrent)
        self.assert_financial_committed()

    def test_concurrently_created_new_year_cache_is_not_overwritten(self):
        path=self.market/'research_2027.json';concurrent=dict(year=2027,securities={'0001':{'manual_tag':'new concurrent file'}})
        def refresh(stage,month):
            self.refresh(stage,month)
            self.write(stage.directory/'market/research_2027.json',dict(year=2027,securities={}))
        def review(*args):self.write(path,concurrent);return True
        result=self.run_current(refresh,review)
        self.assertIn('REFERENCE_CACHE_CONFLICT',result['warnings'])
        self.assertEqual(json.loads(path.read_text()),concurrent)
        self.assert_financial_committed()

    def test_public_cache_write_failure_after_commit_is_warning_not_false_failure(self):
        with patch('update_workflow._publish_reference_cache',side_effect=OSError('Synthetic disk-full')):
            result=self.run_current(self.refresh)
        self.assertIn('REFERENCE_CACHE_UNSAVED',result['warnings'])
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertTrue(result['output'].is_file())
        self.assert_financial_committed()

    def test_default_none_makes_no_reference_request_or_public_write(self):
        result=self.run_current()
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertNotIn('reference',self.order)
        self.assertFalse(any(w.startswith('REFERENCE_') for w in result['warnings']))
        self.assert_financial_committed()

    def test_reference_link_is_neither_followed_nor_published(self):
        target=self.base/'not-public.json';target.write_bytes(self.original)
        self.source.unlink()
        try:self.source.symlink_to(target)
        except OSError:self.skipTest('Links are unavailable in this test environment')
        callback=Mock(side_effect=AssertionError('Do not refresh a linked source'))
        result=self.run_current(callback)
        callback.assert_not_called()
        self.assertIn('REFERENCE_PRICES_UNAVAILABLE',result['warnings'])
        self.assertTrue(self.source.is_symlink());self.assertEqual(target.read_bytes(),self.original)
        self.assert_financial_committed()
