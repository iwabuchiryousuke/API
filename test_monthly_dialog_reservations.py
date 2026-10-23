"""Reservation editor, persistence actions and safe diagnostics without Tk/Windows."""
from copy import deepcopy
from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch

import monthly_dialog as md
from monthly_schedule import JST
from schedule_settings import defaults,delete_schedule,get_schedule,load_settings,save_schedule


NOW=datetime(2026,10,23,20,10,tzinfo=JST)


class ReservationDialogTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.base=Path(self.temporary.name)

    def dialog(self):
        obj=md.MonthlyDialog.__new__(md.MonthlyDialog)
        obj.base=self.base;obj.dialog=MagicMock();obj.app=SimpleNamespace(busy=False,buttons=[],root=MagicMock())
        obj.controls=[];obj.saved_button_states=[];obj.process=None;obj.settings_revision=None
        obj.action_text=MagicMock();obj.logon=MagicMock();obj.logon.get.return_value='ログオン中（標準）'
        obj.logon_initialized=False;obj.next_text=MagicMock();obj.registration_text=MagicMock()
        obj.schedule_tree=MagicMock();obj.schedule_tree.get_children.return_value=[]
        obj.refresh=MagicMock();obj.run_action=MagicMock()
        return obj

    def once(self,stamp=None,**changes):
        stamp=stamp or NOW+timedelta(minutes=30)
        value=dict(kind='once',label='追加の無人取得',time=stamp.strftime('%H:%M'),run_at=stamp.isoformat(),
                   include_pdfs=True,enabled=True)
        value.update(changes)
        return save_schedule(self.base,value)

    def test_defaults_preview_is_read_only_and_deleted_entries_stay_hidden(self):
        settings=load_settings(self.base,create=False)
        self.assertFalse((self.base/'data').exists())
        self.assertEqual([row[1][2] for row in md.schedule_rows(settings)],['毎月末日 16:00','毎月1日 16:00'])
        delete_schedule(self.base,'month-end')
        rows=md.schedule_rows(load_settings(self.base))
        self.assertEqual([row[0] for row in rows],['month-start'])
        self.assertTrue(get_schedule(self.base,'month-end')['deleted'])

    def test_upcoming_uses_changed_times_and_once_and_omits_disabled_deleted(self):
        settings=defaults();settings['schedules'][0]['time']='17:25'
        once=dict(settings['schedules'][0],id='once_example',kind='once',label='当日の予約',
                  time='21:00',run_at='2026-10-23T21:00:00+09:00')
        settings['schedules'].append(once)
        rows=md.upcoming_reservations(settings,NOW)
        self.assertEqual([row[0].isoformat() for row in rows],
            ['2026-10-23T21:00:00+09:00','2026-10-31T17:25:00+09:00','2026-11-01T16:00:00+09:00'])
        settings['schedules'][0]['enabled']=False;once['deleted']=True;once['enabled']=False
        self.assertEqual(len(md.upcoming_reservations(settings,NOW)),1)

    def test_upcoming_does_not_show_missed_once_and_handles_year_leap_boundary(self):
        settings=defaults()
        settings['schedules'].append(dict(settings['schedules'][0],id='once_past',kind='once',
            run_at='2026-10-22T16:00:00+09:00'))
        self.assertEqual(len(md.upcoming_reservations(settings,NOW)),2)
        future=md.upcoming_reservations(defaults(),datetime(2027,12,31,17,tzinfo=JST))
        self.assertEqual([row[0].date().isoformat() for row in future],['2028-01-01','2028-01-31'])
        leap=md.upcoming_reservations(defaults(),datetime(2028,2,28,17,tzinfo=JST))
        self.assertEqual(leap[0][0].date().isoformat(),'2028-02-29')

    def test_once_editor_enforces_real_future_jst_datetime_and_preserves_options(self):
        value=md.edit_schedule_value(None,'  今夜取得  ','2026-10-23','21:05',True,True,NOW)
        self.assertEqual(value['label'],'今夜取得')
        self.assertEqual(value['run_at'],'2026-10-23T21:05:00+09:00')
        self.assertTrue(value['include_pdfs'])
        for day,clock in [('2026-02-30','21:05'),('2026-2-03','21:05'),('2026-10-23','25:05'),('2026-10-23','19:00')]:
            with self.subTest(day=day,clock=clock),self.assertRaises(ValueError):
                md.edit_schedule_value(None,'予約',day,clock,False,True,NOW)
        # Disabling an old reservation is valid; it does not request a replay.
        disabled=md.edit_schedule_value(None,'以前の予約','2026-10-22','20:05',False,False,NOW)
        self.assertFalse(disabled['enabled'])

    def test_recurring_editor_changes_time_without_changing_calendar_kind(self):
        original=defaults()['schedules'][0];before=deepcopy(original)
        value=md.edit_schedule_value(original,'月末の確認','ignored','17:30',True,True,NOW)
        self.assertEqual((value['id'],value['kind'],value['run_at'],value['time']),('month-end','month-end',None,'17:30'))
        self.assertEqual(original,before)
        for label in ('','x'*81,'名前\n欄','名前\u200b欄'):
            with self.assertRaises(ValueError):md.edit_schedule_value(original,label,'','16:00',False,True,NOW)

    def test_save_then_sync_keeps_new_once_and_other_settings(self):
        obj=self.dialog();item=self.once()
        original=get_schedule(self.base,'month-end')
        value=dict(original,time='17:00')
        saved=obj.save_reservation(value,original)
        self.assertEqual(saved['time'],'17:00')
        self.assertEqual(get_schedule(self.base,item['id']),item)
        obj.refresh.assert_called_once();obj.run_action.assert_called_once_with('sync')

    def test_stale_editor_does_not_overwrite_or_start_windows_sync(self):
        obj=self.dialog();original=get_schedule(self.base,'month-end')
        save_schedule(self.base,dict(original,time='18:00'))
        before=(self.base/'data/scheduler/acquisition_schedules.json').read_bytes()
        with self.assertRaises(ValueError):obj.save_reservation(dict(original,time='19:00'),original)
        self.assertEqual((self.base/'data/scheduler/acquisition_schedules.json').read_bytes(),before)
        obj.run_action.assert_not_called()

    def test_toggle_persists_and_syncs_without_restoring_deleted_default(self):
        obj=self.dialog();delete_schedule(self.base,'month-start')
        original=get_schedule(self.base,'month-end');obj.selected_reservation=MagicMock(return_value=original)
        obj.toggle_reservation()
        self.assertFalse(get_schedule(self.base,'month-end')['enabled'])
        self.assertTrue(get_schedule(self.base,'month-start')['deleted'])
        obj.run_action.assert_called_once_with('sync')

    def test_expired_once_cannot_be_reenabled_without_editing_its_date(self):
        obj=self.dialog();original=self.once(datetime(2000,1,1,16,tzinfo=JST),enabled=False)
        obj.selected_reservation=MagicMock(return_value=original)
        with patch.object(md.messagebox,'showinfo') as info:obj.toggle_reservation()
        info.assert_called_once();obj.run_action.assert_not_called()
        self.assertFalse(get_schedule(self.base,original['id'])['enabled'])

    def test_delete_requires_confirmation_and_keeps_a_persistent_tombstone(self):
        obj=self.dialog();original=get_schedule(self.base,'month-end')
        obj.selected_reservation=MagicMock(return_value=original)
        with patch.object(md.messagebox,'askyesno',return_value=False):obj.delete_reservation()
        self.assertFalse(get_schedule(self.base,'month-end')['deleted']);obj.run_action.assert_not_called()
        with patch.object(md.messagebox,'askyesno',return_value=True):obj.delete_reservation()
        self.assertTrue(get_schedule(self.base,'month-end')['deleted'])
        obj.run_action.assert_called_once_with('sync')

    def test_explicit_restore_preserves_additional_once(self):
        obj=self.dialog();once=self.once();delete_schedule(self.base,'month-end')
        with patch.object(md.messagebox,'askyesno',return_value=True):obj.restore_reservations()
        self.assertFalse(get_schedule(self.base,'month-end')['deleted'])
        self.assertEqual(get_schedule(self.base,once['id']),once)
        obj.run_action.assert_called_once_with('sync')

    def test_refresh_uses_configured_dates_and_password_mode_from_registration_record(self):
        obj=self.dialog();settings=load_settings(self.base)
        registration=dict(status='registered',settings_revision=settings['revision'],
            logon_mode='Password',updated_at=NOW.isoformat(),message='ignored arbitrary string')
        with patch('schedule_settings.read_registration_status',return_value=registration):
            self.assertEqual(obj.refresh_reservations(),[])
        obj.logon.set.assert_called_once_with('サインアウト中も実行（パスワード）')
        self.assertNotIn('ignored',str(obj.registration_text.set.call_args))
        self.assertIn('前回のWindows反映は完了',str(obj.registration_text.set.call_args))

    def test_broken_config_is_retained_and_not_replaced_by_defaults(self):
        folder=self.base/'data/scheduler';folder.mkdir(parents=True)
        path=folder/'acquisition_schedules.json';path.write_bytes(b'corrupt')
        obj=self.dialog();warnings=obj.refresh_reservations()
        self.assertTrue(warnings);self.assertEqual(path.read_bytes(),b'corrupt')
        self.assertEqual(obj.schedules,{})

    def test_test_options_are_separate_arguments_and_never_applied_to_scheduled_sync(self):
        obj=self.dialog();obj.run_action=md.MonthlyDialog.run_action.__get__(obj)
        obj.test_show_browser=SimpleNamespace(get=lambda:True);obj.test_include_pdfs=SimpleNamespace(get=lambda:True)
        process=MagicMock();process.poll.return_value=None
        with patch.object(md.os,'name','nt'),patch.object(md.subprocess,'Popen',return_value=process) as popen:
            obj.run_action('test')
        argv=popen.call_args.args[0]
        self.assertIn('--show-browser',argv);self.assertIn('--include-pdfs',argv)
        self.assertNotIn('shell',popen.call_args.kwargs)
        obj.app.busy=False
        with patch.object(md.os,'name','nt'),patch.object(md.subprocess,'Popen',return_value=process) as popen:
            obj.run_action('sync')
        self.assertNotIn('--show-browser',popen.call_args.args[0]);self.assertNotIn('--include-pdfs',popen.call_args.args[0])

    def test_failed_sync_does_not_claim_windows_registered_or_lose_saved_configuration(self):
        obj=self.dialog();once=self.once();obj.process=SimpleNamespace(poll=lambda:1);obj.running_action='sync'
        obj.poll_action('予約反映')
        self.assertIn('設定は保存済み',str(obj.action_text.set.call_args))
        self.assertEqual(get_schedule(self.base,once['id']),once)

    def test_fixed_error_codes_show_actionable_hints_without_raw_errors_or_report_hints(self):
        from monthly_runner import RUN_ERROR_HINTS
        folder=self.base/'data/scheduler';folder.mkdir(parents=True)
        secret='https://invalid.example/?password=never-show'
        value=dict(status='partial',test=True,accounts={'A':dict(asset_status='failed',pdf_status='not-requested',
            error_code='EDGE_LAUNCH_FAILED',error=secret,error_hint=secret),
            'B':dict(asset_status='failed',pdf_status='failed',error_code='UNKNOWN',error=secret,
                     pdf_error_code='SUMMARY_NOT_OBSERVED',pdf_error=secret)})
        (folder/'latest_test_run.json').write_text(json.dumps(value))
        report=md.read_run_status(self.base,True)
        self.assertNotIn('never-show',str(report))
        self.assertEqual(report['rows'][0][-1],RUN_ERROR_HINTS['EDGE_LAUNCH_FAILED'])
        self.assertIn(RUN_ERROR_HINTS['SUMMARY_NOT_OBSERVED'],report['rows'][1][-1])


if __name__=='__main__':unittest.main()
