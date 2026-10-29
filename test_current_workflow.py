"""Four-account current acquisition stages and commits one coherent batch."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from activity import Activities
from asset_snapshots import init as init_assets
from current_capture import load_current_capture
from ledger import Store
from sbi_views import init_observations,save_observation
from update_operations import collect_and_apply,apply_saved
from update_workflow import Cancelled,Workflow,fingerprint,UpdateError

JST=timezone(timedelta(hours=9))
NOW=datetime(2026,10,29,10,5,tzinfo=JST)


class CurrentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);store=Store(self.base/'data');Activities(store);init_assets(store);init_observations(store)
        for account in 'ABCDE':store.set_owner(account,'Example '+account)
        self.initial=fingerprint(store.db);store.close()
        self.auth=Mock(return_value=(Mock(),'anonymous@example.test'))
        self.calls=[];self.partial=False;self.no_fresh=False

    def acquire(self,pw,account,stage,**options):
        self.assertTrue(options['documents']);self.assertTrue(options['capture_assets'])
        self.assertTrue(options['current_only']);self.assertEqual(options['target_dates'],())
        self.calls.append(account)
        if not self.no_fresh:
            values={'cash_total':100,'growth_remaining':{'year':2026,'remaining':200},'tsumitate_remaining':{'year':2026,'remaining':300}}
            save_observation(stage,account,'口座サマリー','',values)
            if not self.partial:save_observation(stage,account,'口座(外貨建)','',{'usd_cash':12.5})
        return dict(complete=True,assets_observed=True,assets_missing=[],saved=0,duplicate=0)

    def run_current(self,review=lambda *args:True,acquire=None):
        with patch('sbi_views._jst_now',return_value=NOW):
            return collect_and_apply(self.base,'2026-10',Mock(),self.auth,acquire or self.acquire,review,current_only=True)

    def test_current_mode_never_replays_or_requests_historical_records(self):
        def review(summary,proposal,prepared):
            stage=next(p for p in prepared.parents if p.name=='prepared').parent
            self.assertEqual(summary['assets_observed'],4)
            self.assertEqual(summary['daily_asset_records'],0)
            self.assertEqual({item['account'] for item in proposal if item['kind']=='現在値'},set('ABCD'))
            self.assertFalse(any('ASSET_DATE_' in warning for warning in summary['warnings']))
            return True
        with patch('acquisition_journal.replay_captures',side_effect=AssertionError('No legacy replay')), \
             patch('asset_history.stage_boundary_observations',side_effect=AssertionError('No historical staging')):
            result=self.run_current(review)
        self.assertEqual(self.calls,list('ABCD'));self.assertEqual(self.auth.call_count,4)
        store=Store(self.base/'data')
        try:
            capture=load_current_capture(store)
            self.assertEqual(capture['captured_at'],NOW.isoformat())
            self.assertEqual(capture['accounts']['A']['values']['usd_cash'],12.5)
            self.assertEqual(store.db.execute('SELECT count(*) FROM asset_snapshots').fetchone()[0],0)
            self.assertEqual(len(store.records()),0)
        finally:store.close()
        self.assertTrue(result['output'].is_file())

    def test_partial_currency_is_blank_warning_not_old_usd_fallback(self):
        self.partial=True
        result=self.run_current()
        store=Store(self.base/'data')
        try:
            batch=load_current_capture(store)
            self.assertNotIn('usd_cash',batch['accounts']['A']['values'])
            self.assertIn('usd_cash',batch['accounts']['A']['missing'])
        finally:store.close()
        self.assertEqual(result['summary']['assets_observed'],0)
        self.assertTrue(any('usd_cash' in item for item in result['warnings']))

    def test_complete_return_without_fresh_evidence_does_not_certify_current_values(self):
        self.no_fresh=True
        result=self.run_current()
        store=Store(self.base/'data')
        try:
            batch=load_current_capture(store)
            self.assertIsNone(batch['captured_at'])
            self.assertTrue(all(not item['values'] for item in batch['accounts'].values()))
        finally:store.close()
        self.assertEqual(result['summary']['assets_observed'],0)

    def test_cancel_does_not_create_live_capture_schema_or_commit_observations(self):
        with self.assertRaises(Cancelled):self.run_current(review=lambda *args:False)
        store=Store(self.base/'data')
        try:
            self.assertEqual(fingerprint(store.db),self.initial)
            self.assertIsNone(load_current_capture(store))
        finally:store.close()

    def test_offline_current_apply_retains_prior_batch_and_skips_legacy_history(self):
        self.run_current()
        store=Store(self.base/'data')
        try:before=load_current_capture(store)
        finally:store.close()
        with patch('acquisition_journal.replay_captures',side_effect=AssertionError('No legacy replay')):
            apply_saved(self.base,'2026-10',lambda *args:True,current_only=True)
        store=Store(self.base/'data')
        try:self.assertEqual(load_current_capture(store),before)
        finally:store.close()

    def test_new_batch_and_old_observations_survive_existing_capture_mutation_refusal(self):
        self.run_current()
        store=Store(self.base/'data')
        try:before=fingerprint(store.db)
        finally:store.close()
        def mutate(pw,account,stage,**options):
            if account=='A':
                with stage.db:stage.db.execute("UPDATE current_acquisitions SET captured_at='2020-01-01T00:00:00+09:00'")
            with patch('sbi_views._jst_now',return_value=NOW+timedelta(minutes=1)):
                return self.acquire(pw,account,stage,**options)
        with patch('sbi_views._jst_now',return_value=NOW+timedelta(minutes=1)):
            with self.assertRaises(UpdateError) as error:
                collect_and_apply(self.base,'2026-10',Mock(),self.auth,mutate,lambda *args:True,current_only=True)
        self.assertEqual(error.exception.code,'STAGING_CHANGED_CAPTURE')
        store=Store(self.base/'data')
        try:self.assertEqual(fingerprint(store.db),before)
        finally:store.close()

    def test_current_mode_requires_all_four_accounts(self):
        with self.assertRaisesRegex(Exception,'4口座'):
            Workflow(self.base).run(collect=Mock(),review=Mock(),accounts=['A'],current_only=True)
