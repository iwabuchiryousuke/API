"""JST occurrence planning and migration entrypoints; no Windows or network."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from monthly_schedule import due_configured_schedule, configured_next_slots
from monthly_records import save_measurement, comparison
from schedule_settings import defaults, save_schedule, delete_schedule


def at(value):return datetime.fromisoformat(value)


class ConfiguredOccurrenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.base=Path(self.temporary.name)

    def test_recurring_custom_time_gate_uses_jst_not_pc_offset(self):
        item=dict(defaults()['schedules'][0],time='17:25')
        self.assertIsNone(due_configured_schedule(item,at('2026-10-31T17:24:59+09:00')))
        value=due_configured_schedule(item,at('2026-10-31T08:25:00+00:00'))
        self.assertEqual(value['scheduled_for'],'2026-10-31T17:25:00+09:00')
        self.assertEqual(value['comparison_month'],'2026-11')

    def test_once_morning_and_late_same_day_never_become_monthly(self):
        item=save_schedule(self.base,dict(run_at='2026-10-23T10:05:00+09:00',label='Morning probe'))
        value=due_configured_schedule(item,at('2026-10-23T10:06:00+09:00'))
        self.assertEqual(value['kind'],'once');self.assertIsNone(value['comparison_month'])
        self.assertIsNone(due_configured_schedule(item,at('2026-10-24T10:06:00+09:00')))

    def test_disabled_deleted_and_stale_revision_cannot_run(self):
        item=defaults()['schedules'][1];now=at('2026-11-01T17:00:00+09:00')
        for value in (dict(item,enabled=False),dict(item,deleted=True,enabled=False)):
            self.assertIsNone(due_configured_schedule(value,now))
        self.assertIsNone(due_configured_schedule(item,now,expected_revision=2))
        with self.assertRaises(ValueError):due_configured_schedule(item,now,expected_revision=True)

    def test_calendar_boundaries_include_leap_day_and_december(self):
        item=dict(defaults()['schedules'][0],time='00:01')
        self.assertIsNone(due_configured_schedule(item,at('2028-02-28T00:01:00+09:00')))
        self.assertEqual(due_configured_schedule(item,at('2028-02-29T00:01:00+09:00'))['comparison_month'],'2028-03')
        self.assertEqual(due_configured_schedule(item,at('2026-12-31T00:01:00+09:00'))['comparison_month'],'2027-01')

    def test_preview_omits_deleted_and_past_once_but_keeps_future_once(self):
        from schedule_settings import load_settings
        save_schedule(self.base,dict(run_at='2026-10-22T10:05:00+09:00',label='Past'))
        future=save_schedule(self.base,dict(run_at='2026-10-24T10:05:00+09:00',label='Future'))
        delete_schedule(self.base,'month-start')
        result=configured_next_slots(load_settings(self.base),at('2026-10-23T19:00:00+09:00'))
        self.assertEqual([row['schedule_id'] for row in result],[future['id'],'month-end'])

    def test_comparison_preserves_distinct_changed_time_attempts_without_mixing(self):
        def add(role,planned,stocks,cash,attempt):
            slot=dict(role=role,scheduled_for=planned,slot_date=planned[:10],comparison_month='2026-11',include_pdfs=False)
            values=dict(summary_stocks=stocks,cash_total=cash)
            missing=[key for key in values if values[key] is None]
            payload=dict(complete=not missing,missing=missing,values=values,
                         observed_at=(at(planned)+timedelta(minutes=5)).isoformat(),displayed_at=None)
            save_measurement(self.base,'A','Example A',slot,payload,attempt_id=attempt)
        add('month-end','2026-10-31T16:00:00+09:00',100,200,'old')
        add('month-end','2026-10-31T17:00:00+09:00',110,210,'edited')
        add('month-end','2026-10-31T18:00:00+09:00',999,None,'partial')
        add('month-start','2026-11-01T17:00:00+09:00',120,240,'start')
        result=comparison(self.base,'2026-11',{'A':'Example A'})['accounts']['A']
        self.assertEqual(result['deltas']['summary_stocks'],10)
        self.assertEqual(result['deltas']['cash_total'],30)
        self.assertEqual(result['scheduled_for']['month-end'],'2026-10-31T17:00:00+09:00')

    def test_automatic_bootstrap_launch_is_noninteractive_and_never_uses_shell(self):
        from app import start_schedule_bootstrap
        launch=Mock()
        process=start_schedule_bootstrap(self.base,popen=launch,platform_name='nt')
        self.assertIs(process,launch.return_value)
        argv=launch.call_args.args[0]
        self.assertEqual(argv[-2:],['--action','bootstrap'])
        self.assertNotIn('shell',launch.call_args.kwargs)
        self.assertNotIn('--logon-mode',argv)
        launch.reset_mock()
        self.assertIsNone(start_schedule_bootstrap(self.base,popen=launch,platform_name='posix'))
        launch.assert_not_called()

    def test_missing_side_uses_current_config_time_but_never_invents_amounts(self):
        save_schedule(self.base,dict(id='month-end',time='17:20'))
        save_schedule(self.base,dict(id='month-start',time='08:30'))
        result=comparison(self.base,'2026-11',{'A':'Example A'})['accounts']['A']
        self.assertEqual(result['scheduled_for']['month-end'],'2026-10-31T17:20:00+09:00')
        self.assertEqual(result['scheduled_for']['month-start'],'2026-11-01T08:30:00+09:00')
        self.assertIsNone(result['month_end']);self.assertIsNone(result['month_start'])
        self.assertIsNone(result['deltas']['summary_stocks'])
