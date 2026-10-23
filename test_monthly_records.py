"""Anonymous immutable monthly-record storage and coherent comparisons."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from monthly_records import (MonthlyRecordError,comparison,load_measurements,
                             save_measurement,select_measurement)


JST=timezone(timedelta(hours=9))
OWNER='Anonymous C'


def slot(role='month-end',month='2026-11'):
    first=datetime.fromisoformat(month+'-01T16:00:00+09:00')
    planned=first-timedelta(days=1) if role=='month-end' else first
    return {'role':role,'scheduled_for':planned.isoformat(),'slot_date':planned.date().isoformat(),
            'comparison_month':month,'include_pdfs':role=='month-start'}


def summary(planned=None,stocks=100,cash=200,delay=300,extras=None):
    planned=planned or slot()
    observed=datetime.fromisoformat(planned['scheduled_for'])+timedelta(seconds=delay)
    values={'summary_stocks':stocks,'cash_total':cash}
    values.update(extras or {})
    missing=[key for key in ('summary_stocks','cash_total') if values[key] is None]
    return {'complete':not missing,'missing':missing,'values':values,
            'observed_at':observed.isoformat(),'displayed_at':None}


def resign(value):
    clean={key:item for key,item in value.items() if key!='sha256'}
    value['sha256']=hashlib.sha256(json.dumps(clean,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return value


class MonthlyRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.ledger=self.base/'data/ledger.sqlite3';self.ledger.parent.mkdir()
        self.ledger.write_bytes(b'opaque-existing-ledger-not-to-be-opened')
        private=self.base/'data/private';private.mkdir()
        (private/'login_C.dat').write_bytes(b'opaque-existing-encrypted-setting')

    def tearDown(self):self.temp.cleanup()

    def save(self,role='month-end',account='C',owner=OWNER,attempt=None,**kwargs):
        planned=slot(role)
        return save_measurement(self.base,account,owner,planned,summary(planned,**kwargs),attempt_id=attempt)

    def test_save_roundtrip_uses_actual_time_and_never_opens_existing_ledger(self):
        before=self.ledger.read_bytes();path=self.save(attempt='fixture-one')
        record=load_measurements(self.base,owners={'C':OWNER})[0]
        self.assertTrue(path.is_file())
        self.assertEqual(record['summary']['values'],{'summary_stocks':100,'cash_total':200})
        self.assertEqual(record['summary']['observed_at'],'2026-10-31T16:05:00+09:00')
        self.assertEqual(record['slot']['scheduled_for'],'2026-10-31T16:00:00+09:00')
        self.assertEqual(record['timing'],'near-schedule');self.assertEqual(record['delay_seconds'],300)
        self.assertFalse(record['displayed_at_is_valuation'])
        self.assertEqual(self.ledger.read_bytes(),before)
        self.assertEqual((self.base/'data/private/login_C.dat').read_bytes(),b'opaque-existing-encrypted-setting')

    def test_explicit_zero_is_complete_but_missing_never_becomes_zero(self):
        self.save(attempt='zero',stocks=0,cash=0)
        self.save('month-start',attempt='missing',stocks=0,cash=None)
        result=comparison(self.base,'2026-11',{'C':OWNER})['accounts']['C']
        self.assertEqual(result['status'],'partial');self.assertFalse(result['complete'])
        self.assertEqual(result['deltas']['summary_stocks'],0)
        self.assertIsNone(result['deltas']['cash_total'])
        self.assertIn('month-start:cash_total',result['missing'])

    def test_missing_optional_nisa_does_not_block_primary_completion(self):
        self.save(extras={'growth_remaining':None})
        selected=select_measurement(self.base,'C',slot(),OWNER)
        self.assertTrue(selected['summary']['complete'])
        self.assertIsNone(selected['summary']['values']['growth_remaining'])
        self.assertNotIn('tsumitate_remaining',selected['summary']['values'])

    def test_total_can_include_other_assets_and_is_not_forced_to_primary_sum(self):
        self.save(extras={'summary_total':350,'broker_cash':120,'bank':80})
        record=select_measurement(self.base,'C',slot(),OWNER)
        self.assertEqual(record['summary']['values']['summary_total'],350)
        self.assertTrue(record['summary']['complete'])

    def test_smaller_total_or_contradictory_explicit_cash_parts_are_rejected(self):
        for extras in ({'summary_total':299},{'broker_cash':120,'bank':81}):
            with self.subTest(extras=extras),self.assertRaises(MonthlyRecordError):self.save(extras=extras)
        self.assertFalse((self.base/'data/monthly').exists())

    def test_cash_parts_are_never_used_to_invent_missing_cash_total(self):
        self.save(cash=None,extras={'broker_cash':120,'bank':80})
        record=select_measurement(self.base,'C',slot(),OWNER)
        self.assertFalse(record['summary']['complete'])
        self.assertIsNone(record['summary']['values']['cash_total'])
        self.assertEqual(record['summary']['missing'],['cash_total'])

    def test_all_numeric_fields_require_nonnegative_finite_numbers(self):
        for value in (True,-1,float('nan'),float('inf'),'100','execute-this'):
            for key in ('summary_stocks','cash_total','summary_total','bank','broker_cash'):
                current=summary();current['values'][key]=value
                with self.subTest(value=str(value),key=key),self.assertRaises(MonthlyRecordError):
                    save_measurement(self.base,'C',OWNER,slot(),current)
        self.assertFalse((self.base/'data/monthly').exists())

    def test_unknown_payload_keys_and_raw_secrets_are_not_saved(self):
        current=summary();current['values']['refresh_token']='fixture-secret'
        with self.assertRaises(MonthlyRecordError):save_measurement(self.base,'C',OWNER,slot(),current)
        current=summary();current['raw_screen']='fixture-secret'
        with self.assertRaises(MonthlyRecordError):save_measurement(self.base,'C',OWNER,slot(),current)
        planned=dict(slot(),slot_id='harmless-added-metadata',page_url='https://example.test/?token=fixture-secret')
        path=save_measurement(self.base,'C',OWNER,planned,summary())
        self.assertNotIn(b'fixture-secret',path.read_bytes())
        self.assertEqual(set(json.loads(path.read_bytes())['slot']),set(slot()))

    def test_nisa_year_and_remaining_are_validated(self):
        for item in ({'year':True,'remaining':1},{'year':'2026','remaining':1},
                     {'year':2026,'remaining':-1},{'year':2026,'remaining':float('inf')},
                     {'year':2026,'remaining':1,'raw_screen':'no'}):
            with self.subTest(item=str(item)),self.assertRaises(MonthlyRecordError):
                self.save(extras={'growth_remaining':item})

    def test_different_nisa_years_do_not_produce_a_delta(self):
        self.save(extras={'growth_remaining':{'year':2026,'remaining':500}})
        self.save('month-start',extras={'growth_remaining':{'year':2027,'remaining':600}})
        result=comparison(self.base,'2026-11',{'C':OWNER})['accounts']['C']
        self.assertIsNone(result['deltas']['growth_remaining'])
        self.assertTrue(result['complete'])

    def test_early_or_wrong_calendar_day_or_naive_observation_is_rejected(self):
        for observed in ('2026-10-31T15:59:59+09:00','2026-11-01T00:01:00+09:00',
                         '2026-10-31T16:05:00'):
            current=summary();current['observed_at']=observed
            with self.subTest(observed=observed),self.assertRaises(MonthlyRecordError):
                save_measurement(self.base,'C',OWNER,slot(),current)

    def test_utc_instants_are_normalized_to_real_jst_slot(self):
        planned=dict(slot(),scheduled_for='2026-10-31T07:00:00+00:00')
        current=summary();current['observed_at']='2026-10-31T07:05:00+00:00'
        save_measurement(self.base,'C',OWNER,planned,current)
        record=select_measurement(self.base,'C',slot(),OWNER)
        self.assertEqual(record['summary']['observed_at'],'2026-10-31T16:05:00+09:00')
        self.assertEqual(record['delay_seconds'],300)

    def test_slot_calendar_and_minute_precision_must_match_role(self):
        for change in ({'role':'other'},{'slot_date':'2026-10-30'},
                       {'scheduled_for':'2026-10-31T15:00:01+09:00'},
                       {'comparison_month':'2026-12'},{'include_pdfs':'true'}):
            with self.subTest(change=change),self.assertRaises(MonthlyRecordError):
                save_measurement(self.base,'C',OWNER,dict(slot(),**change),summary())

    def test_leap_year_and_new_year_slot_dates_are_supported(self):
        for month,expected in (('2028-03','2028-02-29'),('2027-01','2026-12-31')):
            planned=slot(month=month)
            save_measurement(self.base,'C',OWNER,planned,summary(planned))
            record=select_measurement(self.base,'C',planned,OWNER)
            self.assertEqual(record['slot']['slot_date'],expected)

    def test_idempotent_same_attempt_does_not_overwrite_or_accept_changed_values(self):
        path=self.save(attempt='same-attempt');before=path.read_bytes()
        self.assertEqual(self.save(attempt='same-attempt'),path)
        with self.assertRaises(MonthlyRecordError):self.save(attempt='same-attempt',stocks=999)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(len(load_measurements(self.base)),1)

    def test_default_ids_preserve_all_attempts_but_invalid_given_ids_are_rejected(self):
        self.save();self.save()
        self.assertEqual(len(load_measurements(self.base)),2)
        for identifier in ('','../escape','a/b','a:b',False):
            with self.subTest(identifier=identifier),self.assertRaises(MonthlyRecordError):
                self.save(attempt=identifier)

    def test_load_and_select_empty_storage_do_not_create_any_monthly_files(self):
        self.assertEqual(load_measurements(self.base),[])
        self.assertIsNone(select_measurement(self.base,'C',slot(),OWNER))
        self.assertFalse((self.base/'data/monthly').exists())

    def test_owner_binding_and_account_separation(self):
        self.save(account='A',owner='Anonymous A',stocks=111)
        self.save(stocks=333)
        selected=select_measurement(self.base,'C',slot(),'Anonymous\u3000C')
        self.assertEqual(selected['summary']['values']['summary_stocks'],333)
        with self.assertRaises(MonthlyRecordError):select_measurement(self.base,'C',slot(),'Other owner')
        with self.assertRaises(MonthlyRecordError):comparison(self.base,'2026-11',{'A':OWNER,'C':OWNER})
        with self.assertRaises(MonthlyRecordError):self.save(account='E')

    def test_checksum_failure_and_recomputed_unknown_schema_both_fail_closed(self):
        path=self.save();original=path.read_bytes()
        data=json.loads(original);data['summary']['values']['summary_stocks']=999
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(MonthlyRecordError,'チェックサム'):load_measurements(self.base)
        data=json.loads(original);data['credentials']={'token':'not-allowed'}
        path.write_text(json.dumps(resign(data)))
        with self.assertRaises(MonthlyRecordError):load_measurements(self.base)

    def test_filename_and_payload_account_cannot_be_rebound(self):
        path=self.save(attempt='fixture')
        data=json.loads(path.read_bytes());data['account']='A'
        path.write_text(json.dumps(resign(data)))
        with self.assertRaisesRegex(MonthlyRecordError,'名義'):load_measurements(self.base)

    def test_symlink_record_and_parent_are_refused_without_touching_outside(self):
        outside=self.base/'outside.json';outside.write_bytes(b'private-outside')
        path=self.save();path.unlink()
        try:path.symlink_to(outside)
        except (OSError,NotImplementedError):self.skipTest('Symlinks unavailable')
        with self.assertRaises(MonthlyRecordError):load_measurements(self.base)
        self.assertEqual(outside.read_bytes(),b'private-outside')
        path.unlink();folder=path.parent;folder.rmdir()
        folder.symlink_to(self.base/'data/private',target_is_directory=True)
        with self.assertRaises(MonthlyRecordError):self.save()
        self.assertEqual((self.base/'data/private/login_C.dat').read_bytes(),b'opaque-existing-encrypted-setting')

    def test_atomic_replace_failure_preserves_prior_success_and_cleans_temporary_files(self):
        existing=self.save(attempt='successful');before=existing.read_bytes()
        with patch('monthly_records.os.replace',side_effect=OSError('simulated disk error')):
            with self.assertRaises(MonthlyRecordError):self.save(attempt='new-attempt')
        self.assertEqual(existing.read_bytes(),before)
        self.assertEqual(list(existing.parent.glob('*.json')),[existing])
        self.assertEqual(list(existing.parent.glob('*.tmp')),[])

    def test_file_fsync_failure_does_not_publish_a_success_record(self):
        with patch('monthly_records.os.fsync',side_effect=OSError('simulated disk error')):
            with self.assertRaises(MonthlyRecordError):self.save()
        self.assertEqual(load_measurements(self.base),[])

    def test_select_prefers_complete_coherent_attempt_and_never_mixes_partial_fields(self):
        self.save(attempt='stocks-only',stocks=100,cash=None,delay=60)
        self.save(attempt='cash-only',stocks=None,cash=200,delay=120)
        self.save('month-start',attempt='after',stocks=110,cash=250)
        pair=comparison(self.base,'2026-11',{'C':OWNER})['accounts']['C']
        self.assertEqual(pair['month_end']['attempt_id'],'stocks-only')
        self.assertEqual(pair['deltas']['summary_stocks'],10)
        self.assertIsNone(pair['deltas']['cash_total'])
        self.assertEqual(pair['status'],'partial')
        self.save(attempt='complete-late',stocks=101,cash=201,delay=3600)
        selected=select_measurement(self.base,'C',slot(),OWNER)
        self.assertEqual(selected['attempt_id'],'complete-late')

    def test_new_late_retry_never_replaces_an_existing_complete_near_measurement(self):
        self.save(attempt='near',stocks=100,delay=600)
        self.save(attempt='later-near',stocks=200,delay=1200)
        self.save(attempt='late',stocks=300,delay=3600)
        selected=select_measurement(self.base,'C',slot(),OWNER)
        self.assertEqual(selected['attempt_id'],'near')
        self.assertEqual(len(load_measurements(self.base)),3)

    def test_pair_comparison_retains_actual_times_signed_deltas_and_lateness(self):
        self.save(stocks=100,cash=200)
        self.save('month-start',stocks=90,cash=240,delay=3600)
        result=comparison(self.base,'2026-11',{'C':OWNER})
        pair=result['accounts']['C']
        self.assertEqual(pair['deltas']['summary_stocks'],-10)
        self.assertEqual(pair['deltas']['cash_total'],40)
        self.assertEqual(pair['status'],'late');self.assertTrue(pair['complete'])
        self.assertEqual(pair['observed_at']['month-start'],'2026-11-01T17:00:00+09:00')
        self.assertEqual(pair['scheduled_for']['month-start'],'2026-11-01T16:00:00+09:00')
        self.assertIn('運用損益ではありません',result['note'])
        self.assertIsNone(pair['deltas']['summary_total'])

    def test_missing_role_produces_gaps_not_fake_deltas(self):
        self.save()
        pair=comparison(self.base,'2026-11',{'C':OWNER})['accounts']['C']
        self.assertEqual(pair['status'],'missing');self.assertFalse(pair['complete'])
        self.assertIsNone(pair['month_start'])
        self.assertTrue(all(value is None for value in pair['deltas'].values()))

    def test_page_clock_never_becomes_valuation_or_observation_time(self):
        current=summary();current['displayed_at']='2026/10/30 15:00'
        save_measurement(self.base,'C',OWNER,slot(),current)
        record=select_measurement(self.base,'C',slot(),OWNER)
        self.assertEqual(record['summary']['observed_at'],'2026-10-31T16:05:00+09:00')
        self.assertEqual(record['summary']['displayed_at'],'2026/10/30 15:00')
        self.assertFalse(record['displayed_at_is_valuation'])

    def test_large_integer_yen_retains_a_one_yen_difference(self):
        large=10**30
        self.save(stocks=large,cash=0)
        self.save('month-start',stocks=large+1,cash=0)
        result=comparison(self.base,'2026-11',{'C':OWNER})['accounts']['C']
        self.assertEqual(result['month_start']['summary']['values']['summary_stocks'],large+1)
        self.assertEqual(result['deltas']['summary_stocks'],1)

    def test_large_integer_cash_parts_are_reconciled_without_rounding_away_a_yen(self):
        with self.assertRaises(MonthlyRecordError):
            self.save(cash=10**30,extras={'broker_cash':10**30,'bank':1})


if __name__=='__main__':unittest.main()
