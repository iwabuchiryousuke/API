"""Configured public runner integration using synthetic local records only."""
from datetime import datetime,timedelta
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_login import AcquisitionError
from login_credentials import AuthenticationRequired
from monthly_records import load_measurements
from monthly_runner import RUN_ERROR_HINTS
from schedule_settings import delete_schedule,get_schedule,save_schedule
from scheduled_run_store import ScheduledRunStore
from test_monthly_runner_integration import JST,MonthlyRunnerFixture


class ConfiguredRunner(MonthlyRunnerFixture):
    def setUp(self):
        super().setUp()
        self.current=datetime(2026,10,23,10,6,tzinfo=JST)

    def reserve(self,include_pdfs=True):
        return save_schedule(self.base,dict(id='probe-once',kind='once',
            label='合成データでの単発取得',run_at='2026-10-23T10:05:00+09:00',
            enabled=True,deleted=False,include_pdfs=include_pdfs))

    def run_reserved(self,item,**overrides):
        return self.run_monthly(schedule_id=item['id'],schedule_revision=item['revision'],**overrides)

    def record_store(self,result):
        return ScheduledRunStore(self.base,result['slot'],{a:'Example '+a for a in 'ABCD'})

    def test_arbitrary_morning_occurrence_captures_assets_then_optional_pdfs(self):
        item=self.reserve()
        result=self.run_reserved(item)
        self.assertEqual(result['exit_code'],0)
        self.assertEqual(result['slot']['scheduled_for'],'2026-10-23T10:05:00+09:00')
        self.assertEqual(self.calls,[('asset',a) for a in 'ABCD']+[('pdf',a) for a in 'ABCD'])
        self.assertEqual(len(self.observations()),4)
        self.assertEqual(self.protected_rows(),self.before)
        store=self.record_store(result)
        for account in 'ABCD':
            self.assertEqual(store.select(account)['summary']['observed_at'],self.current.isoformat())
            self.assertTrue(store.pdf_done(account))
        self.assertEqual(load_measurements(self.base),[])
        self.assertIsNone(result['comparison']);self.assertIsNone(result['comparison_csv'])
        self.assertFalse((self.base/'output/monthly').exists())

    def test_configured_pdf_off_never_starts_pdf_phase(self):
        item=self.reserve(include_pdfs=False)
        result=self.run_reserved(item)
        self.assertEqual(result['exit_code'],0)
        self.assertEqual(self.calls,[('asset',a) for a in 'ABCD'])
        self.assertTrue(all(row['pdf_status']=='not-requested' for row in result['accounts'].values()))

    def test_scheduled_policy_cannot_be_overridden_by_test_only_options(self):
        item=self.reserve(include_pdfs=False)
        for options in ({'include_pdfs':True},{'show_browser':True}):
            with self.subTest(options=options):
                result=self.run_reserved(item,**options)
                self.assertNotEqual(result['exit_code'],0)
                self.assertEqual(result['error_code'],'SCHEDULE_CONFIGURATION')
        self.assertEqual(self.calls,[]);self.assertEqual(self.auth_calls,[])
        self.assertEqual(self.observations(),[])

    def test_before_planned_time_and_wrong_calendar_day_do_no_authentication(self):
        item=self.reserve()
        for stamp in ('2026-10-23T10:04:59+09:00','2026-10-24T10:06:00+09:00',
                      '2026-10-22T10:06:00+09:00'):
            with self.subTest(stamp=stamp):
                self.current=datetime.fromisoformat(stamp)
                result=self.run_reserved(item)
                self.assertEqual(result['status'],'skipped')
                self.assertEqual(result['exit_code'],0)
        self.assertEqual(self.calls,[]);self.assertEqual(self.auth_calls,[])
        self.assertEqual(self.observations(),[])

    def test_stale_revision_disabled_and_deleted_items_do_no_work(self):
        original=self.reserve()
        changed=save_schedule(self.base,dict(original,label='改訂した合成予約'),
                              expected_revision=original['revision'])
        self.assertEqual(self.run_reserved(original)['status'],'skipped')
        disabled=save_schedule(self.base,dict(changed,enabled=False),
                               expected_revision=changed['revision'])
        self.assertEqual(self.run_reserved(disabled)['status'],'skipped')
        deleted=delete_schedule(self.base,disabled['id'])
        self.assertEqual(self.run_reserved(deleted)['status'],'skipped')
        self.assertEqual(self.calls,[]);self.assertEqual(self.auth_calls,[])
        self.assertEqual(self.observations(),[])

    def test_completed_occurrence_is_reused_without_authentication_or_duplicate_rows(self):
        item=self.reserve()
        first=self.run_reserved(item)
        self.assertEqual(first['exit_code'],0)
        observed=self.observations()
        self.calls.clear();self.auth_calls.clear();self.current+=timedelta(minutes=1)
        second=self.run_reserved(item)
        self.assertEqual(second['exit_code'],0)
        self.assertEqual(self.calls,[]);self.assertEqual(self.auth_calls,[])
        self.assertEqual(self.observations(),observed)
        self.assertTrue(all(row['asset_status']=='reused' and row['pdf_status']=='reused'
                            for row in second['accounts'].values()))

    def test_changed_revision_creates_new_coherent_evidence_instead_of_reusing_old(self):
        item=self.reserve(include_pdfs=False)
        first=self.run_reserved(item)
        self.assertEqual(first['exit_code'],0)
        revised=save_schedule(self.base,dict(item,label='改訂した合成予約'),
                              expected_revision=item['revision'])
        self.calls.clear();self.auth_calls.clear();self.current+=timedelta(minutes=1)
        second=self.run_reserved(revised)
        self.assertEqual(second['exit_code'],0)
        self.assertEqual(self.calls,[('asset',a) for a in 'ABCD'])
        self.assertEqual(len(self.observations()),8)
        self.assertNotEqual(self.record_store(first).key,self.record_store(second).key)
        self.assertEqual(self.record_store(first).select('A')['summary']['observed_at'],
                         (self.current-timedelta(minutes=1)).isoformat())
        self.assertEqual(self.record_store(second).select('A')['summary']['observed_at'],
                         self.current.isoformat())
        self.assertEqual(self.protected_rows(),self.before)

    def test_edited_month_end_and_start_times_appear_in_monthly_comparison(self):
        end=get_schedule(self.base,'month-end')
        end=save_schedule(self.base,dict(end,time='17:00'),expected_revision=end['revision'])
        self.current=datetime(2026,10,31,17,4,tzinfo=JST)
        end_result=self.run_reserved(end)
        self.assertEqual(end_result['exit_code'],0)
        start=get_schedule(self.base,'month-start')
        start=save_schedule(self.base,dict(start,time='17:05',include_pdfs=False),
                            expected_revision=start['revision'])
        self.calls.clear()
        self.current=datetime(2026,11,1,17,9,tzinfo=JST)
        start_result=self.run_reserved(start)
        self.assertEqual(start_result['exit_code'],0)
        self.assertEqual(self.calls,[('asset',a) for a in 'ABCD'])
        for row in start_result['comparison']['accounts'].values():
            self.assertTrue(row['complete'])
            self.assertEqual(row['scheduled_for']['month-end'],'2026-10-31T17:00:00+09:00')
            self.assertEqual(row['scheduled_for']['month-start'],'2026-11-01T17:05:00+09:00')
            self.assertEqual(row['observed_at']['month-start'],self.current.isoformat())
        self.assertEqual(len(load_measurements(self.base)),8)
        self.assertTrue(Path(start_result['comparison_csv']).is_file())
        self.assertEqual(self.protected_rows(),self.before)

    def test_deleting_reservation_during_capture_stops_before_current_commit_or_next_account(self):
        item=self.reserve()
        acquire=self.acquire
        def remove(playwright,account,stage=None,**options):
            result=acquire(playwright,account,stage,**options)
            if account=='A' and not options['documents']:delete_schedule(self.base,item['id'])
            return result
        result=self.run_reserved(item,acquire=remove)
        self.assertNotEqual(result['exit_code'],0)
        self.assertEqual(result['error_code'],'SCHEDULE_CHANGED')
        self.assertEqual(self.calls,[('asset','A')])
        self.assertEqual(self.observations(),[])
        self.assertIsNone(self.record_store(result).select('A'))
        self.assertEqual(self.protected_rows(),self.before)

    def test_editing_reservation_during_capture_stops_before_current_commit_or_next_account(self):
        item=self.reserve()
        acquire=self.acquire
        def revise(playwright,account,stage=None,**options):
            result=acquire(playwright,account,stage,**options)
            if account=='A' and not options['documents']:
                save_schedule(self.base,dict(item,time='10:07',run_at='2026-10-23T10:07:00+09:00'),
                              expected_revision=item['revision'])
            return result
        result=self.run_reserved(item,acquire=revise)
        self.assertNotEqual(result['exit_code'],0)
        self.assertEqual(result['error_code'],'SCHEDULE_CHANGED')
        self.assertEqual(self.calls,[('asset','A')])
        self.assertEqual(self.observations(),[])
        self.assertIsNone(self.record_store(result).select('A'))

    def test_fixed_safe_error_hints_do_not_copy_raw_browser_exception_or_url(self):
        item=self.reserve(include_pdfs=False)
        acquire=self.acquire
        raw='https://example.invalid/private-session-token anonymous raw failure'
        def broken(playwright,account,stage=None,**options):
            if account=='A':raise AcquisitionError('SUMMARY_NOT_OBSERVED',raw)
            return acquire(playwright,account,stage,**options)
        result=self.run_reserved(item,acquire=broken)
        self.assertNotEqual(result['exit_code'],0)
        row=result['accounts']['A']
        self.assertEqual(row['error_code'],'SUMMARY_NOT_OBSERVED')
        self.assertEqual(row['error_hint'],RUN_ERROR_HINTS['SUMMARY_NOT_OBSERVED'])
        report=Path(result['run_path']).read_text(encoding='utf-8')
        self.assertNotIn(raw,report);self.assertNotIn('private-session-token',report)
        self.assertEqual([row['account'] for row in self.observations()],list('BCD'))

    def test_explicit_visible_test_forwards_headless_false_without_consuming_occurrence(self):
        item=self.reserve()
        seen=[];acquire=self.acquire
        def visible(playwright,account,stage=None,**options):
            seen.append(options['headless'])
            return acquire(playwright,account,stage,**dict(options,headless=True))
        tested=self.run_monthly(test=True,show_browser=True,include_pdfs=True,acquire=visible)
        self.assertEqual(tested['exit_code'],0)
        self.assertEqual(seen,[False]*8)
        self.assertIn('test_runs',Path(tested['run_path']).parts)
        self.calls.clear();self.auth_calls.clear();self.current+=timedelta(minutes=1)
        production=self.run_reserved(item)
        self.assertEqual(production['exit_code'],0)
        self.assertEqual(self.calls,[('asset',a) for a in 'ABCD']+[('pdf',a) for a in 'ABCD'])

    def test_manual_auth_hold_blocks_repeated_login_until_explicit_successful_test(self):
        item=self.reserve()
        acquire=self.acquire
        def manual(playwright,account,stage=None,**options):
            if account=='A' and not options['documents']:
                self.calls.append(('manual','A'))
                raise AuthenticationRequired('Synthetic manual auth required')
            return acquire(playwright,account,stage,**options)
        first=self.run_reserved(item,acquire=manual)
        self.assertNotEqual(first['exit_code'],0)
        self.assertEqual(first['accounts']['A']['asset_status'],'needs-attention')
        self.assertEqual(first['accounts']['A']['pdf_status'],'blocked')
        self.assertTrue(self.record_store(first).held('A'))
        self.calls.clear();self.auth_calls.clear();self.current+=timedelta(minutes=1)
        retry=self.run_reserved(item)
        self.assertNotEqual(retry['exit_code'],0)
        self.assertEqual(self.calls,[]);self.assertEqual(self.auth_calls,[])
        self.current+=timedelta(minutes=1)
        tested=self.run_monthly(test=True)
        self.assertEqual(tested['exit_code'],0)
        self.assertFalse(self.record_store(first).held('A'))
        self.calls.clear();self.auth_calls.clear();self.current+=timedelta(minutes=1)
        complete=self.run_reserved(item)
        self.assertEqual(complete['exit_code'],0)
        self.assertEqual(self.calls,[('asset','A'),('pdf','A')])
        self.assertEqual(self.protected_rows(),self.before)

    def test_old_native_role_actions_obey_saved_deletion_and_edited_time(self):
        from monthly_runner import main
        self.current=datetime(2026,10,31,16,7,tzinfo=JST)
        with patch('monthly_runner.BASE',self.base), \
             patch('monthly_runner.os',SimpleNamespace(name='nt')), \
             patch('monthly_runner._now',side_effect=lambda:self.current), \
             patch('monthly_runner.run_monthly',return_value={'status':'completed','exit_code':0}) as worker, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--role','month-end','--scheduled']),0)
            self.assertEqual(worker.call_args.kwargs['schedule_id'],'month-end')
            worker.reset_mock()
            delete_schedule(self.base,'month-end')
            self.assertEqual(main(['--role','month-end','--scheduled']),0)
            worker.assert_not_called()
            old=get_schedule(self.base,'month-end')
            save_schedule(self.base,dict(old,enabled=True,deleted=False,time='17:00'),
                          expected_revision=old['revision'])
            self.assertEqual(main(['--role','auto','--scheduled']),0)
            worker.assert_not_called()
            self.current=self.current.replace(hour=17)
            self.assertEqual(main(['--role','auto','--scheduled']),0)
            self.assertEqual(worker.call_args.kwargs['schedule_id'],'month-end')
