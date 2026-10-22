"""Daily observation tests use synthetic amounts, clocks and staging stores."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest

from asset_history import (HistoryError,FIELDS,daily_records,boundary_candidates,
                           stage_boundary_observations)
from asset_snapshots import exact_fields
from ledger import Store
from sbi_views import init_observations


JST=timezone(timedelta(hours=9))
FIRST='2026-10-01';LAST='2026-09-30'


def asset(value=100):
    return {'domestic':value,'fund':0,'bond':0,'foreign':200,'usd_fund':0,
            'usd_bond':0,'cash':10,'bank':20,'foreign_cash':40,'usd_cash':0.5}


def summary(cash=30):
    return {'cash_total':cash,'growth_remaining':{'year':2026,'remaining':1000},
            'tsumitate_remaining':{'year':2026,'remaining':0}}


class DailyHistory(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'stage');self.addCleanup(self.store.close)
        init_observations(self.store)

    def record(self,day=FIRST,clock='09:00:00',kind='My資産',values=None,
               displayed='same',offset='+09:00',account='C',payload_time=None):
        stamp=day+'T'+clock+offset
        shown=day.replace('-','/')+' '+clock if displayed=='same' else displayed
        payload={'source':kind,'observed_at':payload_time or stamp,
                 'displayed_at':shown,'values':asset() if values is None else values}
        with self.store.db:self.store.db.execute('INSERT INTO account_observations VALUES (?,?,?,?)',
             (account,kind,stamp,json.dumps(payload,ensure_ascii=False)))
        return stamp

    def window(self,day=FIRST,start='08:59:00',end='09:10:00',account='C'):
        return {'account':account,'since':day+'T'+start+'+09:00','acquired_at':day+'T'+end+'+09:00'}

    def candidate(self,day=FIRST,windows=()):
        return boundary_candidates(self.store,'C',(day,),acquisition_windows=windows)[day]

    def snapshot(self,day=FIRST,source='手動確認',values=None,evidence=None):
        self.store.db.execute('CREATE TABLE IF NOT EXISTS asset_snapshots(account TEXT,as_of TEXT,payload TEXT,PRIMARY KEY(account,as_of))')
        payload=dict({key:None for key in FIELDS},source=source,note='keep original',recorded_at='2026-10-22T10:00:00+09:00')
        payload.update(values or {})
        if evidence is not None:payload['evidence']=evidence
        with self.store.db:self.store.db.execute('INSERT INTO asset_snapshots VALUES (?,?,?)',('C',day,json.dumps(payload)))
        return self.payload(day)

    def payload(self,day=FIRST):
        row=self.store.db.execute('SELECT payload FROM asset_snapshots WHERE account=? AND as_of=?',('C',day)).fetchone()
        return json.loads(row[0]) if row else None

    def test_daily_records_preserve_every_original_clock_and_order(self):
        self.record(clock='17:00:00',values=asset(120));self.record(clock='08:00:00')
        rows=daily_records(self.store,'C',FIRST)
        self.assertEqual([r['observed_at'][11:19] for r in rows],['08:00:00','17:00:00'])
        self.assertEqual(rows[0]['displayed_at'],'2026/10/01 08:00:00')
        self.assertFalse(rows[0]['displayed_at_is_valuation'])

    def test_utc_timestamp_is_grouped_by_actual_jst_day(self):
        self.record(day=LAST,clock='15:01:00',offset='+00:00',displayed='2026/10/01 00:01:00')
        self.assertEqual(daily_records(self.store,'C',LAST),[])
        rows=daily_records(self.store,'C',FIRST)
        self.assertEqual(rows[0]['observed_at'],'2026-10-01T00:01:00+09:00')

    def test_first_day_selects_first_asset_observation_not_latest(self):
        self.record(clock='17:00:00',values=asset(999));self.record(clock='08:00:00',values=asset(111))
        chosen=self.candidate()
        self.assertEqual(chosen['values']['domestic'],111)
        self.assertEqual(chosen['selection'],'first-observed-in-day')
        self.assertFalse(chosen['exact_boundary'])

    def test_month_end_selects_last_asset_observation_not_close_claim(self):
        self.record(day=LAST,clock='08:00:00',values=asset(111));self.record(day=LAST,clock='22:00:00',values=asset(999))
        chosen=self.candidate(LAST)
        self.assertEqual(chosen['values']['domestic'],999)
        self.assertEqual(chosen['selection'],'last-observed-in-day')
        self.assertFalse(chosen['exact_boundary'])

    def test_missing_target_day_is_not_filled_from_another_day(self):
        self.record(day='2026-10-22')
        chosen=self.candidate(LAST)
        self.assertEqual(chosen['available'],[])
        self.assertIn('NO_SAME_DAY_OBSERVATION',chosen['warnings'])

    def test_displayed_clock_on_another_day_is_preserved_but_excluded(self):
        self.record(displayed='2026/09/30 23:59:00')
        rows=daily_records(self.store,'C',FIRST)
        self.assertEqual(rows[0]['source_date_status'],'different-day-clock')
        chosen=self.candidate()
        self.assertEqual(chosen['available'],[])
        self.assertIn('DIFFERENT_OR_INVALID_SOURCE_DAY_EXCLUDED',chosen['warnings'])

    def test_absent_displayed_clock_is_an_observation_not_exact_valuation(self):
        self.record(displayed=None)
        chosen=self.candidate()
        self.assertEqual(chosen['values']['domestic'],100)
        self.assertEqual(chosen['records'][0]['source_date_status'],'absent')
        self.assertFalse(chosen['displayed_at_is_valuation'])

    def test_nearby_independent_partial_observations_are_not_merged_without_window(self):
        self.record(clock='09:00:00',kind='口座サマリー',values=summary())
        self.record(clock='09:03:00')
        chosen=self.candidate()
        self.assertEqual(chosen['coherence'],'single-observation')
        self.assertIsNone(chosen['values']['growth_remaining'])
        self.assertEqual(len(chosen['records']),1)

    def test_explicit_same_batch_merges_partial_records_with_real_time_range(self):
        self.record(clock='09:00:00',kind='口座サマリー',values=summary())
        self.record(clock='09:03:00')
        chosen=self.candidate(windows=(self.window(),))
        self.assertEqual(chosen['coherence'],'journal-window')
        self.assertEqual(chosen['values']['growth_remaining'],1000)
        self.assertEqual(chosen['values']['tsumitate_remaining'],0)
        self.assertEqual(chosen['observed_from'][11:19],'09:00:00')
        self.assertEqual(chosen['observed_to'][11:19],'09:03:00')
        self.assertFalse(chosen['exact_boundary'])

    def test_two_batches_do_not_mix_morning_and_late_summary(self):
        self.record(day=LAST,clock='08:00:00')
        self.record(day=LAST,clock='23:59:00',kind='口座サマリー',values=summary(9000))
        windows=(self.window(LAST,'07:59:00','08:10:00'),self.window(LAST,'23:58:00','23:59:30'))
        chosen=self.candidate(LAST,windows)
        self.assertEqual(chosen['values']['cash'],30)
        self.assertIsNone(chosen['values']['growth_remaining'])
        self.assertEqual(chosen['observed_to'][11:19],'08:00:00')

    def test_cross_midnight_batch_does_not_merge_previous_day_summary(self):
        self.record(day=LAST,clock='23:59:00',kind='口座サマリー',values=summary())
        self.record(day=FIRST,clock='00:01:00')
        window={'account':'C','since':LAST+'T23:58:00+09:00','acquired_at':FIRST+'T00:05:00+09:00'}
        chosen=self.candidate(windows=(window,))
        self.assertIsNone(chosen['values']['growth_remaining'])
        self.assertTrue(all(r['observed_day']==FIRST for r in chosen['records']))

    def test_stale_summary_source_day_cannot_fill_new_day_nisa(self):
        self.record(clock='09:00:00',kind='口座サマリー',values=summary(),displayed='2026/09/30 23:59:00')
        self.record(clock='09:03:00')
        chosen=self.candidate(windows=(self.window(),))
        self.assertIsNone(chosen['values']['growth_remaining'])

    def test_same_window_contradictory_cash_blocks_staging(self):
        self.record(clock='09:00:00',kind='口座サマリー',values=summary(999))
        self.record(clock='09:03:00')
        chosen=self.candidate(windows=(self.window(),))
        self.assertIn('cash',chosen['conflicts'])
        result=stage_boundary_observations(self.store,'C',(FIRST,),acquisition_windows=(self.window(),))
        self.assertEqual(result['count'],0)
        self.assertTrue(any('CONFLICT' in w for w in result['warnings']))

    def test_same_instant_timezone_alias_conflict_is_not_chosen_arbitrarily(self):
        self.record(clock='09:00:00',values=asset(100))
        self.record(clock='00:00:00',offset='+00:00',values=asset(200),displayed='2026/10/01 09:00:00')
        self.assertIn('domestic',self.candidate()['conflicts'])

    def test_missing_components_stay_unknown_and_explicit_zero_is_kept(self):
        self.record(values={'domestic':100,'foreign':200,'cash':0,'foreign_cash':0,'usd_cash':0})
        chosen=self.candidate()
        for key in ('domestic','foreign','cash'):self.assertIsNone(chosen['values'][key])
        self.assertEqual(chosen['values']['foreign_cash'],0)
        self.assertEqual(chosen['usd_cash'],0)

    def test_native_usd_is_never_a_yen_amount(self):
        self.record(kind='口座(外貨建)',values={'usd_cash':123.45})
        chosen=self.candidate()
        self.assertEqual(chosen['usd_cash'],123.45)
        self.assertIsNone(chosen['values']['foreign_cash'])

    def test_nisa_year_does_not_carry_into_new_year(self):
        self.record(day='2027-01-01',kind='口座サマリー',values=summary())
        chosen=self.candidate('2027-01-01')
        self.assertIsNone(chosen['values']['growth_remaining'])
        self.assertIn('NISA_YEAR_DIFFERS:growth_remaining',chosen['warnings'])

    def test_invalid_amounts_fail_without_database_changes(self):
        for value in [True,-1,float('inf')]:
            with self.subTest(value=value):
                with self.store.db:self.store.db.execute('DELETE FROM account_observations')
                self.record(values={'foreign_cash':value})
                before=self.store.db.total_changes
                with self.assertRaises(HistoryError):self.candidate()
                self.assertEqual(self.store.db.total_changes,before)

    def test_payload_time_mismatch_is_rejected(self):
        self.record(payload_time=FIRST+'T10:00:00+09:00')
        with self.assertRaises(HistoryError):daily_records(self.store,'C',FIRST)

    def test_manual_snapshot_including_blanks_wins(self):
        old=self.snapshot(values={'cash':777});self.record()
        result=stage_boundary_observations(self.store,'C',(FIRST,))
        self.assertEqual(result['preserved'],[FIRST]);self.assertEqual(result['count'],0)
        self.assertEqual(self.payload(),old)

    def test_explicit_dated_snapshot_including_blanks_wins(self):
        old=self.snapshot(source='SBI 指定日資産',values={'cash':777},evidence={'method':'displayed-date','fields':['cash'],'as_of':FIRST})
        self.record()
        self.assertEqual(stage_boundary_observations(self.store,'C',(FIRST,))['count'],0)
        self.assertEqual(self.payload(),old)

    def test_all_blank_exact_snapshot_is_still_preserved(self):
        old=self.snapshot(source='SBI My資産履歴');self.record()
        self.assertEqual(stage_boundary_observations(self.store,'C',(FIRST,))['count'],0)
        self.assertEqual(self.payload(),old)

    def test_stage_note_and_evidence_do_not_claim_exact_boundary(self):
        self.record();result=stage_boundary_observations(self.store,'C',(FIRST,))
        self.assertEqual(result['count'],1)
        row=self.payload()
        self.assertEqual(row['source'],'SBI 当日実測')
        self.assertEqual(row['evidence']['method'],'observed-at')
        self.assertIn('09:00:00',row['note'])
        self.assertIn('月末終値・月初開始時点ではない',row['note'])
        self.assertEqual(exact_fields(row),set())

    def test_repeat_staging_is_idempotent(self):
        self.record();stage_boundary_observations(self.store,'C',(FIRST,));before=self.payload()
        self.assertEqual(stage_boundary_observations(self.store,'C',(FIRST,))['count'],0)
        self.assertEqual(self.payload(),before)

    def test_previous_current_day_snapshot_can_be_replaced_by_labelled_measurement(self):
        self.snapshot(source='SBI取得当日',values={'cash':999});self.record()
        self.assertEqual(stage_boundary_observations(self.store,'C',(FIRST,))['count'],1)
        self.assertEqual(self.payload()['cash'],30)

    def test_overlapping_batch_membership_does_not_merge(self):
        self.record(clock='09:00:00',kind='口座サマリー',values=summary());self.record(clock='09:03:00')
        windows=(self.window(),self.window(start='08:58:00',end='09:11:00'))
        chosen=self.candidate(windows=windows)
        self.assertIsNone(chosen['values']['growth_remaining'])
        self.assertIn('AMBIGUOUS_ACQUISITION_WINDOW',chosen['warnings'])

    def test_nonboundary_date_is_not_automatically_staged(self):
        self.record(day='2026-10-22')
        chosen=self.candidate('2026-10-22')
        self.assertIsNone(chosen['selection']);self.assertEqual(chosen['available'],[])
        self.assertEqual(stage_boundary_observations(self.store,'C',('2026-10-22',))['count'],0)

    def test_preview_does_not_create_snapshot_table(self):
        self.record();before=self.store.db.total_changes
        daily_records(self.store,'C',FIRST);self.candidate()
        self.assertEqual(self.store.db.total_changes,before)
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='asset_snapshots'").fetchone())

    def test_other_account_window_does_not_allow_merging(self):
        self.record(clock='09:00:00',kind='口座サマリー',values=summary());self.record(clock='09:03:00')
        chosen=self.candidate(windows=(self.window(account='A'),))
        self.assertIsNone(chosen['values']['growth_remaining'])

    def test_invalid_window_fails_before_staging(self):
        self.record()
        with self.assertRaises(HistoryError):
            stage_boundary_observations(self.store,'C',(FIRST,),acquisition_windows=(self.window(start='10:00:00',end='09:00:00'),))
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='asset_snapshots'").fetchone())

    def test_empty_existing_manual_payload_is_preserved(self):
        self.snapshot()
        with self.store.db:self.store.db.execute('UPDATE asset_snapshots SET payload=?',('{}',))
        self.record()
        self.assertEqual(stage_boundary_observations(self.store,'C',(FIRST,))['count'],0)
        self.assertEqual(self.payload(),{})

    def test_staging_failure_rolls_back_every_planned_day(self):
        self.record(day=FIRST);self.record(day=LAST)
        original=self.store.db
        class FailingDb:
            inserts=0
            def execute(self,sql,*args):
                if sql.startswith('INSERT OR REPLACE INTO asset_snapshots'):
                    self.inserts+=1
                    if self.inserts==2:raise RuntimeError('injected second insert failure')
                return original.execute(sql,*args)
        self.store.db=FailingDb()
        try:
            with self.assertRaises(RuntimeError):stage_boundary_observations(self.store,'C',(LAST,FIRST))
        finally:self.store.db=original
        self.assertIsNone(original.execute("SELECT 1 FROM sqlite_master WHERE name='asset_snapshots'").fetchone())

    def test_usd_only_record_does_not_hide_usable_summary_partial_candidate(self):
        self.record(clock='09:00:00',kind='口座サマリー',values=summary())
        self.record(clock='09:03:00',values={'usd_cash':123.45})
        chosen=self.candidate()
        self.assertEqual(chosen['values']['cash'],30)
        self.assertEqual(chosen['values']['growth_remaining'],1000)
        self.assertIsNone(chosen['values']['domestic'])


if __name__=='__main__':unittest.main()
