"""JST calendar and real-observation timing tests; no network or SBI login."""
from datetime import datetime,timezone
import unittest

from monthly_schedule import due_slot,next_slots,observation_timeliness


def at(value):return datetime.fromisoformat(value)


class MonthlyScheduleTests(unittest.TestCase):
    def test_actual_month_end_after_16_has_exact_six_field_slot(self):
        slot=due_slot(at('2026-10-31T16:00:00+09:00'))
        self.assertEqual(slot,{'role':'month-end','scheduled_for':'2026-10-31T16:00:00+09:00',
            'slot_date':'2026-10-31','comparison_month':'2026-11','include_pdfs':False})

    def test_month_start_includes_pdfs_and_current_comparison_month(self):
        slot=due_slot(at('2026-11-01T16:05:00+09:00'))
        self.assertEqual(slot['role'],'month-start');self.assertTrue(slot['include_pdfs'])
        self.assertEqual(slot['comparison_month'],'2026-11')

    def test_before_16_is_not_due(self):
        for stamp in ['2026-10-31T15:59:59.999999+09:00','2026-11-01T00:00:00+09:00']:
            self.assertIsNone(due_slot(at(stamp)))

    def test_timezone_conversion_uses_jst_calendar_not_utc_day(self):
        slot=due_slot(at('2026-10-31T07:00:00+00:00'))
        self.assertEqual(slot['slot_date'],'2026-10-31')
        self.assertIsNone(due_slot(at('2026-10-31T15:00:00+00:00')))
        # The UTC timestamp above is already November 1st at 00:00 JST.

    def test_different_pc_offset_still_uses_jst_16(self):
        slot=due_slot(at('2026-10-31T00:00:00-07:00'))
        self.assertEqual(slot['scheduled_for'],'2026-10-31T16:00:00+09:00')

    def test_role_mismatch_and_ordinary_day_never_replay_missed_slot(self):
        self.assertIsNone(due_slot(at('2026-11-01T17:00:00+09:00'),'month-end'))
        self.assertIsNone(due_slot(at('2026-10-31T17:00:00+09:00'),'month-start'))
        self.assertIsNone(due_slot(at('2026-11-02T17:00:00+09:00'),'auto'))

    def test_leap_year_february_and_century_rules(self):
        self.assertIsNone(due_slot(at('2028-02-28T16:00:00+09:00')))
        self.assertEqual(due_slot(at('2028-02-29T16:00:00+09:00'))['role'],'month-end')
        self.assertEqual(due_slot(at('2100-02-28T16:00:00+09:00'))['role'],'month-end')
        self.assertEqual(due_slot(at('2000-02-29T16:00:00+09:00'))['role'],'month-end')

    def test_weekend_is_not_rolled_to_friday(self):
        # 2026-10-31 is Saturday. Friday is an ordinary nonboundary day.
        self.assertIsNone(due_slot(at('2026-10-30T16:00:00+09:00')))
        self.assertEqual(due_slot(at('2026-10-31T16:00:00+09:00'))['slot_date'],'2026-10-31')

    def test_december_end_compares_next_january(self):
        self.assertEqual(due_slot(at('2026-12-31T16:00:00+09:00'))['comparison_month'],'2027-01')

    def test_naive_clock_and_unknown_role_rejected(self):
        with self.assertRaises(ValueError):due_slot(datetime(2026,10,31,16))
        with self.assertRaises(ValueError):due_slot(at('2026-10-31T16:00:00+09:00'),'end')
        with self.assertRaises(ValueError):due_slot(at('2026-10-31T16:00:00+09:00'),allow_early='false')

    def test_early_debug_override_does_not_make_observation_on_time(self):
        now=at('2026-10-31T15:00:00+09:00');slot=due_slot(now,allow_early=True)
        status=observation_timeliness(slot['scheduled_for'],now)
        self.assertEqual(status['status'],'early');self.assertFalse(status['within_16_window'])

    def test_timeliness_exact_and_30min_are_on_time_but_not_exact16_claim(self):
        scheduled='2026-10-31T16:00:00+09:00'
        for observed in [scheduled,'2026-10-31T16:30:00+09:00']:
            status=observation_timeliness(scheduled,observed)
            self.assertEqual(status['status'],'on-time');self.assertTrue(status['within_16_window'])
            self.assertFalse(status['exact_16_claim'])

    def test_31min_late_is_not_labelled_16_measurement(self):
        status=observation_timeliness('2026-10-31T16:00:00+09:00','2026-10-31T16:31:00+09:00')
        self.assertEqual(status['status'],'late');self.assertFalse(status['within_16_window'])

    def test_next_day_clock_never_valid_for_previous16_slot(self):
        status=observation_timeliness('2026-10-31T16:00:00+09:00','2026-11-01T16:00:00+09:00')
        self.assertEqual(status['status'],'different-day');self.assertFalse(status['same_day'])
        self.assertFalse(status['within_16_window'])

    def test_early_and_microsecond_outside_tolerance_are_distinct(self):
        scheduled='2026-10-31T16:00:00+09:00'
        self.assertEqual(observation_timeliness(scheduled,'2026-10-31T15:59:59+09:00')['status'],'early')
        self.assertEqual(observation_timeliness(scheduled,'2026-10-31T16:30:00.000001+09:00')['status'],'late')

    def test_invalid_scheduled_boundary_and_tolerance_rejected(self):
        for scheduled in ['2026-10-30T16:00:00+09:00','2026-10-31T15:00:00+09:00']:
            with self.assertRaises(ValueError):observation_timeliness(scheduled,'2026-10-31T16:00:00+09:00')
        for tolerance in [-1,True,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):observation_timeliness('2026-10-31T16:00:00+09:00','2026-10-31T16:00:00+09:00',tolerance)

    def test_next_two_slots_before_month_end(self):
        slots=next_slots(at('2026-10-23T18:00:00+09:00'))
        self.assertEqual([s['slot_date'] for s in slots],['2026-10-31','2026-11-01'])

    def test_next_slots_after_boundary16_does_not_claim_oldslot_upcoming(self):
        slots=next_slots(at('2026-10-31T16:00:00.000001+09:00'))
        self.assertEqual([s['slot_date'] for s in slots],['2026-11-01','2026-11-30'])

    def test_next_slots_include_exact_current_slot_and_handle_leap_year(self):
        slots=next_slots(at('2028-02-29T16:00:00+09:00'))
        self.assertEqual([s['slot_date'] for s in slots],['2028-02-29','2028-03-01'])

    def test_next_slots_across_year_and_offset(self):
        slots=next_slots(at('2026-12-31T07:01:00+00:00'))
        self.assertEqual([s['slot_date'] for s in slots],['2027-01-01','2027-01-31'])
        self.assertEqual(next_slots(at('2026-10-23T16:00:00+09:00'),0),[])

    def test_next_slots_bad_count_rejected(self):
        for count in [-1,True,1.5,241]:
            with self.assertRaises(ValueError):next_slots(at('2026-10-23T16:00:00+09:00'),count)


if __name__=='__main__':unittest.main()
