"""Retirement migration uses opaque synthetic data, never native Windows tasks."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import windows_tasks as tasks
from schedule_retirement import (ScheduleRetirementError,is_retired,mark_native_result,
                                 prepare_retirement,read_retirement,retire_legacy_schedules)
from schedule_settings import (get_schedule,load_settings,restore_defaults,save_schedule,
                               settings_path)


class ScheduleRetirement(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.base=Path(self.temporary.name)/'new application';self.base.mkdir()
        self.opaque={
            'data/ledger.sqlite3':b'opaque SQLite fixture; must never be opened',
            'data/private/sbi_A.enc':b'opaque encrypted private fixture',
            'data/monthly/measurements/old.json':b'opaque prior month history',
            'data/scheduled/measurements/old.json':b'opaque prior reservation history',
            'data/電子ポストPDF/A/old.pdf':b'opaque original PDF fixture',
            'output/existing.xlsx':b'opaque existing workbook fixture',
        }
        for name,content in self.opaque.items():
            path=self.base/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
        self.commands=[]

    def unchanged(self):
        self.assertEqual({name:(self.base/name).read_bytes() for name in self.opaque},self.opaque)

    def intercept(self,command,**options):
        script=Path(command[command.index('-File')+1])
        config=Path(command[command.index('-ConfigFile')+1])
        self.assertTrue(script.read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertFalse(options.get('shell',False))
        self.commands.append((command,script,config,json.loads(config.read_text(encoding='utf-8'))))
        return Mock(returncode=0)

    def receipt(self,old_root,identity=None):
        settings=load_settings(self.base,create=True)
        tasks._save_registration(self.base,settings,'Password','registered')
        path=self.base/'data/scheduler/task_registration.json'
        value=json.loads(path.read_text(encoding='utf-8'));value['app_root']=str(old_root)
        if identity is not None:value['identity_key']=identity
        path.write_text(json.dumps(value),encoding='utf-8')
        return path

    def test_prepare_disables_all_preserving_preferences_and_financial_files(self):
        end=save_schedule(self.base,dict(id='month-end',time='17:25'))
        once=save_schedule(self.base,dict(label='Synthetic once reservation',run_at='2026-12-15T09:20:00+09:00'))
        prior=load_settings(self.base)
        state=prepare_retirement(self.base)
        self.assertTrue(is_retired(self.base));self.assertEqual(state['preferences_status'],'disabled')
        after=load_settings(self.base)
        self.assertTrue(all(item['deleted'] and not item['enabled'] for item in after['schedules']))
        self.assertEqual(get_schedule(self.base,end['id'])['time'],'17:25')
        self.assertEqual(get_schedule(self.base,once['id'])['run_at'],once['run_at'])
        self.assertEqual(get_schedule(self.base,once['id'])['label'],once['label'])
        self.assertGreater(after['revision'],prior['revision']);self.unchanged()

    def test_local_retirement_is_idempotent_without_erasing_old_settings(self):
        prepare_retirement(self.base)
        first=settings_path(self.base).read_bytes()
        prepare_retirement(self.base)
        self.assertEqual(settings_path(self.base).read_bytes(),first)
        self.assertTrue(is_retired(self.base));self.unchanged()

    def test_gate_precedes_preference_failure_and_bad_settings_are_preserved(self):
        folder=self.base/'data/scheduler';folder.mkdir()
        path=folder/'acquisition_schedules.json';path.write_bytes(b'invalid preserved settings')
        state=prepare_retirement(self.base)
        self.assertTrue(is_retired(self.base))
        self.assertEqual(state['preferences_status'],'unreadable')
        self.assertEqual(state['error_code'],'SETTINGS_UNREADABLE')
        self.assertEqual(path.read_bytes(),b'invalid preserved settings');self.unchanged()

    def test_non_windows_startup_only_marks_local_retirement(self):
        launcher=Mock()
        result=retire_legacy_schedules(self.base,popen=launcher,platform_name='posix')
        launcher.assert_not_called()
        self.assertEqual(result['status'],'disabled-locally')
        self.assertEqual(read_retirement(self.base)['native_status'],'not-windows');self.unchanged()

    def test_windows_startup_is_async_and_intent_is_saved_before_launch(self):
        def launch(command,**options):
            self.assertTrue(is_retired(self.base))
            self.assertTrue(all(item['deleted'] for item in load_settings(self.base)['schedules']))
            self.assertEqual(command[-2:],['--action','retire'])
            self.assertNotIn('shell',options)
            self.assertNotIn('--logon-mode',command)
            self.assertEqual(options['cwd'],str(self.base))
            return sentinel
        sentinel=object()
        result=retire_legacy_schedules(self.base,popen=launch,platform_name='nt')
        self.assertIs(result['process'],sentinel)
        self.assertEqual(result['status'],'native-cleanup-started')
        self.assertEqual(read_retirement(self.base)['native_status'],'started');self.unchanged()

    def test_launch_failure_never_reports_native_tasks_removed(self):
        result=retire_legacy_schedules(self.base,popen=Mock(side_effect=OSError('synthetic path or secret')),
                                       platform_name='nt')
        state=read_retirement(self.base)
        self.assertEqual(result['status'],'failed');self.assertEqual(state['native_status'],'failed')
        self.assertEqual(state['error_code'],'PROCESS_START_FAILED')
        self.assertNotIn('synthetic',json.dumps(state));self.assertTrue(is_retired(self.base));self.unchanged()

    def test_status_save_failure_cannot_crash_startup_or_launch_a_process(self):
        launcher=Mock()
        with patch('schedule_retirement.mark_native_result',side_effect=OSError('synthetic disk failure')):
            result=retire_legacy_schedules(self.base,popen=launcher,platform_name='nt')
        self.assertEqual(result['status'],'failed');launcher.assert_not_called()
        self.assertTrue(is_retired(self.base));self.unchanged()

    def test_copied_receipt_adds_only_exact_previous_root_for_current_identity(self):
        old=self.base.parent/'previous application';old.mkdir()
        original=old/'must-not-change.txt';original.write_bytes(b'old folder remains opaque')
        receipt=self.receipt(old)
        before=receipt.read_bytes()
        state=prepare_retirement(self.base)
        self.assertEqual(state['app_roots'],[str(self.base),str(old)])
        self.assertEqual(receipt.read_bytes(),before)
        self.assertEqual(original.read_bytes(),b'old folder remains opaque');self.unchanged()

    def test_other_identity_receipt_cannot_expand_native_cleanup_scope(self):
        self.receipt(self.base.parent/'another identity application',identity='0'*64)
        state=prepare_retirement(self.base)
        self.assertEqual(state['app_roots'],[str(self.base)])
        self.assertEqual(state['error_code'],'IDENTITY_UNAVAILABLE');self.unchanged()

    def test_copied_retirement_marker_cannot_reuse_another_identity_roots(self):
        self.receipt(self.base.parent/'previous application')
        prepare_retirement(self.base)
        marker=self.base/'data/scheduler/automatic_schedules_retired.json'
        value=json.loads(marker.read_text(encoding='utf-8'));value['identity_key']='0'*64
        marker.write_text(json.dumps(value),encoding='utf-8')
        receipt=self.base/'data/scheduler/task_registration.json'
        old=json.loads(receipt.read_text(encoding='utf-8'));old['identity_key']='0'*64
        receipt.write_text(json.dumps(old),encoding='utf-8')
        state=prepare_retirement(self.base)
        self.assertEqual(state['app_roots'],[str(self.base)]);self.unchanged()

    def test_corrupt_receipt_is_preserved_and_does_not_prevent_local_gate(self):
        folder=self.base/'data/scheduler';folder.mkdir()
        path=folder/'task_registration.json';path.write_bytes(b'opaque corrupt receipt')
        state=prepare_retirement(self.base)
        self.assertTrue(is_retired(self.base));self.assertEqual(state['app_roots'],[str(self.base)])
        self.assertEqual(state['error_code'],'IDENTITY_UNAVAILABLE')
        self.assertEqual(path.read_bytes(),b'opaque corrupt receipt');self.unchanged()

    def test_corrupt_retirement_marker_is_not_overwritten_or_treated_as_enabled(self):
        folder=self.base/'data/scheduler';folder.mkdir()
        path=folder/'automatic_schedules_retired.json';path.write_bytes(b'opaque corrupt retirement')
        with self.assertRaises(ScheduleRetirementError):is_retired(self.base)
        result=retire_legacy_schedules(self.base,popen=Mock(),platform_name='nt')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(path.read_bytes(),b'opaque corrupt retirement');self.unchanged()

    def test_retirement_marker_symlink_does_not_modify_target(self):
        folder=self.base/'data/scheduler';folder.mkdir()
        outside=self.base.parent/'outside';outside.write_bytes(b'keep')
        try:(folder/'automatic_schedules_retired.json').symlink_to(outside)
        except OSError:self.skipTest('symlinks unavailable')
        launcher=Mock();result=retire_legacy_schedules(self.base,popen=launcher,platform_name='nt')
        self.assertEqual(result['status'],'failed');launcher.assert_not_called()
        self.assertEqual(outside.read_bytes(),b'keep');self.unchanged()

    def test_old_registration_actions_cannot_resurrect_after_retirement(self):
        prepare_retirement(self.base);restore_defaults(self.base)
        self.assertTrue(all(item['enabled'] for item in load_settings(self.base)['schedules']))
        for action in ('install','sync','bootstrap'):
            with self.subTest(action=action),redirect_stdout(io.StringIO()):
                runner=Mock()
                self.assertEqual(tasks.run_windows_action(action,self.base,runner=runner,platform_name='nt'),0)
                runner.assert_not_called()
        self.assertTrue(is_retired(self.base));self.unchanged()

    def test_native_retirement_payload_is_scoped_and_temporary_files_are_removed(self):
        old=self.base.parent/'previous application';self.receipt(old)
        self.assertEqual(tasks.run_windows_action('retire',self.base,runner=self.intercept,platform_name='nt'),0)
        command,script,config,payload=self.commands[0]
        self.assertEqual(command[command.index('-Action')+1],'retire')
        self.assertEqual(payload['RetireRoots'],[str(self.base),str(old)])
        self.assertTrue(all(item['Deleted'] for item in payload['tasks']))
        self.assertFalse(script.exists());self.assertFalse(config.exists())
        self.assertEqual(read_retirement(self.base)['native_status'],'removed');self.unchanged()

    def test_native_failure_records_failure_while_preserving_local_retirement(self):
        self.assertEqual(tasks.run_windows_action('retire',self.base,
            runner=Mock(return_value=Mock(returncode=7)),platform_name='nt'),7)
        state=read_retirement(self.base)
        self.assertEqual(state['native_status'],'failed');self.assertEqual(state['error_code'],'NATIVE_REMOVAL_FAILED')
        self.assertTrue(all(item['deleted'] for item in load_settings(self.base)['schedules']));self.unchanged()

    def test_native_launch_exception_has_fixed_failure_metadata(self):
        with self.assertRaises(OSError):
            tasks.run_windows_action('retire',self.base,runner=Mock(side_effect=OSError('synthetic secret')),platform_name='nt')
        self.assertEqual(read_retirement(self.base)['error_code'],'PROCESS_START_FAILED')
        self.assertNotIn('synthetic',json.dumps(read_retirement(self.base)));self.unchanged()

    def test_native_cleanup_can_proceed_without_overwriting_corrupt_preferences(self):
        folder=self.base/'data/scheduler';folder.mkdir()
        path=folder/'acquisition_schedules.json';path.write_bytes(b'preserved corrupt preferences')
        self.assertEqual(tasks.run_windows_action('retire',self.base,runner=self.intercept,platform_name='nt'),0)
        self.assertEqual(path.read_bytes(),b'preserved corrupt preferences')
        state=read_retirement(self.base)
        self.assertEqual(state['native_status'],'removed');self.assertEqual(state['preferences_status'],'unreadable')
        self.assertEqual(state['error_code'],'SETTINGS_UNREADABLE');self.unchanged()

    def test_native_cleanup_waits_for_registration_then_uses_its_final_state(self):
        real=tasks._registration_lock;attempts=[]
        def lock(base):
            attempts.append(base)
            if len(attempts)==1:raise tasks.RegistrationBusy('Synthetic active registration')
            return real(base)
        with patch('windows_tasks._registration_lock',side_effect=lock),patch('windows_tasks.time.sleep') as pause:
            self.assertEqual(tasks.run_windows_action('retire',self.base,runner=self.intercept,platform_name='nt'),0)
        pause.assert_called_once_with(.2)
        self.assertEqual(len(self.commands),1)
        self.assertEqual(read_retirement(self.base)['native_status'],'removed');self.unchanged()

    def test_busy_retirement_is_local_first_and_never_runs_native_changes(self):
        runner=Mock()
        with patch('windows_tasks._registration_lock',side_effect=tasks.RegistrationBusy('busy')), \
             patch('windows_tasks.time.sleep'):
            self.assertEqual(tasks.run_windows_action('retire',self.base,runner=runner,platform_name='nt'),1)
        runner.assert_not_called();self.assertTrue(is_retired(self.base))
        self.assertEqual(read_retirement(self.base)['error_code'],'REGISTRATION_BUSY');self.unchanged()

    def test_native_source_checks_exact_ownership_before_stop_or_delete(self):
        source=tasks.PS_SOURCE
        retire=source.split("if($Action -eq 'retire') {",1)[1].split(' $existing=@{}',1)[0]
        self.assertIn('$taskUser -ne $currentSid',retire)
        self.assertIn("$task.Name.StartsWith($scopePrefix+'_'",retire)
        self.assertIn("$description -eq ('SBI Family Trades monthly; app='+$scopeRoot",retire)
        self.assertIn('$description.StartsWith($acquisitionPrefix',retire)
        self.assertIn("if($task.Name -eq ($scopePrefix+'_'+$suffix)) { $owned=$true }",retire)
        self.assertLess(retire.index('if(-not $owned)'),retire.index('$task.Enabled=$false'))
        self.assertLess(retire.index('$task.Enabled=$false'),retire.index('$task.Stop(0)'))
        self.assertLess(retire.index('$task.Stop(0)'),retire.index('$folder.DeleteTask'))
        self.assertNotIn('Get-Credential',retire)
        self.assertNotIn('RegisterTaskDefinition',retire)
        self.assertLess(source.index("if($Action -eq 'retire')"),source.index("if([TimeZoneInfo]::Local.Id"))
