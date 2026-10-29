"""Current batches are coherent, owner-bound, and do not backfill blanks."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from current_capture import (CurrentCaptureError,FIELDS,account_capture,
    load_current_capture,observation_identities,save_current_capture)
from ledger import Store
from sbi_views import init_observations,save_observation

JST=timezone(timedelta(hours=9))
NOW=datetime(2026,10,29,10,5,tzinfo=JST)


class CurrentCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.store=Store(self.base/'data');self.addCleanup(self.store.close)
        for account in 'ABCD':self.store.set_owner(account,'Example '+account)
        init_observations(self.store)

    def observe(self,account='A',kind='口座サマリー',values=None,when=NOW):
        with patch('sbi_views._jst_now',return_value=when):
            save_observation(self.store,account,kind,'',values or {})

    def capture(self,account='A',before=None):
        return account_capture(self.store,account,'Example '+account,set() if before is None else before,NOW,NOW+timedelta(minutes=1))

    def complete(self,account='A',when=NOW):
        self.observe(account,values={'cash_total':0,'growth_remaining':{'year':2026,'remaining':100},'tsumitate_remaining':{'year':2026,'remaining':200}},when=when)
        self.observe(account,'口座(外貨建)',{'usd_cash':12.5},when=when)

    def test_read_only_absence_does_not_create_new_schema(self):
        self.assertIsNone(load_current_capture(self.store))
        self.assertFalse(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='current_acquisitions'").fetchone())

    def test_native_usd_and_explicit_zero_survive_with_actual_source_times(self):
        self.complete()
        result=self.capture()
        self.assertEqual(result['values']['cash_total'],0)
        self.assertEqual(result['values']['usd_cash'],12.5)
        self.assertEqual(result['missing'],[])
        self.assertEqual(result['observed_at'],NOW.isoformat())
        self.assertEqual(set(result['source_observed_at']),{'口座サマリー','口座(外貨建)'})

    def test_yen_valuation_and_my_assets_are_not_usd_native_cash(self):
        self.observe(values={'cash_total':100,'usd_cash':999,'foreign_cash':500})
        self.observe(kind='My資産',values={'usd_cash':888})
        result=self.capture()
        self.assertEqual(result['values'],{'cash_total':100})
        self.assertIn('usd_cash',result['missing'])

    def test_preexisting_rows_are_never_relabelled_fresh(self):
        self.complete(when=NOW-timedelta(days=1))
        before=observation_identities(self.store,'A')
        result=self.capture(before=before)
        self.assertEqual(result['values'],{})
        self.assertEqual(result['missing'],list(FIELDS))
        self.assertIsNone(result['observed_at'])

    def test_latest_summary_does_not_borrow_an_earlier_attempt_nisa_field(self):
        self.complete()
        self.observe(values={'cash_total':150},when=NOW+timedelta(seconds=1))
        result=self.capture()
        self.assertEqual(result['values'],{'cash_total':150,'usd_cash':12.5})
        self.assertIn('growth_remaining',result['missing'])
        self.assertIn('tsumitate_remaining',result['missing'])

    def test_wrong_nisa_year_is_missing_without_changing_its_original_observation(self):
        self.observe(values={'cash_total':100,'growth_remaining':{'year':2025,'remaining':200}})
        result=self.capture()
        self.assertNotIn('growth_remaining',result['values'])
        raw=json.loads(self.store.db.execute('SELECT payload FROM account_observations').fetchone()[0])
        self.assertEqual(raw['values']['growth_remaining']['year'],2025)

    def test_future_or_stale_new_rows_cannot_enter_a_current_batch(self):
        for index,when in enumerate((NOW+timedelta(days=1),NOW-timedelta(days=1))):
            with self.subTest(when=when):
                self.observe(account='AB'[index],values={'cash_total':100},when=when)
                with self.assertRaises(CurrentCaptureError):self.capture(account='AB'[index])

    def test_existing_observation_rewrite_is_rejected(self):
        self.observe(values={'cash_total':100})
        before=observation_identities(self.store,'A')
        with self.store.db:
            self.store.db.execute("UPDATE account_observations SET payload=replace(payload,'100','999') WHERE account='A'")
        with self.assertRaises(CurrentCaptureError):self.capture(before=before)

    def test_latest_partial_batch_never_falls_back_to_prior_complete_batch(self):
        accounts={}
        for account in 'ABCD':
            self.complete(account);accounts[account]=self.capture(account)
        saved=save_current_capture(self.store,'complete',accounts,NOW)
        self.assertEqual(load_current_capture(self.store),saved)
        empty={account:self.capture(account,before=observation_identities(self.store,account)) for account in 'ABCD'}
        save_current_capture(self.store,'partial',empty,NOW+timedelta(minutes=1))
        latest=load_current_capture(self.store)
        self.assertEqual(latest['batch_id'],'partial');self.assertIsNone(latest['captured_at'])
        self.assertTrue(all(not item['values'] for item in latest['accounts'].values()))

    def test_changed_owner_rejects_saved_batch_without_overwriting_records(self):
        accounts={}
        for account in 'ABCD':self.complete(account);accounts[account]=self.capture(account)
        save_current_capture(self.store,'bound',accounts,NOW)
        raw=self.store.db.execute('SELECT payload FROM current_acquisitions').fetchone()[0]
        self.store.set_owner('A','Different owner')
        with self.assertRaises(CurrentCaptureError):load_current_capture(self.store)
        self.assertEqual(self.store.db.execute('SELECT payload FROM current_acquisitions').fetchone()[0],raw)

    def test_saved_batch_checksum_tamper_is_rejected(self):
        empty={account:self.capture(account) for account in 'ABCD'}
        save_current_capture(self.store,'bound',empty,NOW)
        with self.store.db:self.store.db.execute("UPDATE current_acquisitions SET payload=replace(payload,'ExampleA','ModifiedA')")
        with self.assertRaises(CurrentCaptureError):load_current_capture(self.store)

    def test_same_batch_identifier_is_idempotent_only_when_unchanged(self):
        empty={account:self.capture(account) for account in 'ABCD'}
        first=save_current_capture(self.store,'same',empty,NOW)
        self.assertEqual(save_current_capture(self.store,'same',empty,NOW),first)
        with self.assertRaises(CurrentCaptureError):save_current_capture(self.store,'same',empty,NOW+timedelta(seconds=1))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM current_acquisitions').fetchone()[0],1)
