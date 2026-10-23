"""Configured native task specs and process boundaries; no live Windows tasks."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import windows_tasks as wt
from schedule_settings import (delete_schedule,get_schedule,load_settings,
                               read_registration_status,save_schedule)


class ConfiguredWindowsTasks(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.base=Path(self.temporary.name)
        self.calls=[]

    def intercepted(self,command,**options):
        script=Path(command[command.index('-File')+1])
        config=Path(command[command.index('-ConfigFile')+1])
        self.calls.append((command,script,config,json.loads(config.read_text('utf-8'))))
        self.assertTrue(script.read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertFalse(options.get('shell',False))
        return Mock(returncode=0)

    def test_configured_builtin_time_and_once_trigger_have_revision_bound_actions(self):
        item=get_schedule(self.base,'month-end')
        save_schedule(self.base,dict(item,time='18:25'))
        once=save_schedule(self.base,dict(run_at='2026-10-25T09:15:00+09:00',include_pdfs=True))
        plan=wt.build_configured_plan(self.base,now=datetime.fromisoformat('2026-10-23T20:00:00+09:00'))
        end,first,extra=plan['tasks']
        self.assertEqual(end['start_boundary'],'2026-10-23T18:25:00+09:00')
        self.assertEqual(end['start_boundary_time'],'18:25:00+09:00')
        self.assertEqual(end['schedule_revision'],2)
        self.assertEqual(extra['trigger_type'],1)
        self.assertEqual(extra['start_boundary'],once['run_at'])
        self.assertIn('--schedule-revision',extra['arguments'])
        self.assertEqual(extra['arguments'][-1],'--scheduled')
        self.assertNotIn('--role',extra['arguments'])

    def test_delete_and_disable_are_preserved_in_pure_task_specs(self):
        delete_schedule(self.base,'month-end')
        save_schedule(self.base,dict(get_schedule(self.base,'month-start'),enabled=False))
        end,first=wt.build_configured_plan(self.base)['tasks']
        self.assertTrue(end['deleted']);self.assertFalse(end['enabled'])
        self.assertFalse(first['deleted']);self.assertFalse(first['enabled'])

    def test_valid_hyphenated_once_id_is_valid_for_native_task_suffix(self):
        save_schedule(self.base,dict(id='probe-once',run_at='2026-11-08T21:00:00+09:00'))
        plan=wt.build_configured_plan(self.base)
        payload=wt._registration_payload(plan)
        self.assertEqual(payload['tasks'][-1]['Suffix'],'Once_probe-once')
        self.assertIn("$item.Suffix -notmatch '^[A-Za-z0-9_-]{1,80}$'",wt.PS_SOURCE)

    def test_bootstrap_registers_once_and_skip_never_restores_a_deleted_schedule(self):
        self.assertEqual(wt.run_windows_action('bootstrap',self.base,runner=self.intercepted,platform_name='nt'),0)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(read_registration_status(self.base)['status'],'registered')
        self.assertEqual(wt.run_windows_action('bootstrap',self.base,runner=self.intercepted,platform_name='nt'),0)
        self.assertEqual(len(self.calls),1)
        delete_schedule(self.base,'month-end')
        self.assertEqual(wt.run_windows_action('bootstrap',self.base,runner=self.intercepted,platform_name='nt'),0)
        self.assertEqual(len(self.calls),2)
        self.assertTrue(self.calls[-1][3]['tasks'][0]['Deleted'])
        self.assertTrue(get_schedule(self.base,'month-end')['deleted'])

    def test_sync_payload_and_scripts_are_removed_after_success(self):
        once=save_schedule(self.base,dict(run_at='2026-11-08T21:00:00+09:00'))
        self.assertEqual(wt.run_windows_action('sync',self.base,runner=self.intercepted,platform_name='nt'),0)
        _,script,config,payload=self.calls[0]
        self.assertEqual(payload['tasks'][-1]['Id'],once['id'])
        self.assertFalse(script.exists());self.assertFalse(config.exists())

    def test_failed_native_sync_preserves_settings_and_records_failure(self):
        load_settings(self.base,create=True)
        before=(self.base/'data/scheduler/acquisition_schedules.json').read_bytes()
        runner=Mock(return_value=Mock(returncode=8))
        self.assertEqual(wt.run_windows_action('sync',self.base,runner=runner,platform_name='nt'),8)
        self.assertEqual((self.base/'data/scheduler/acquisition_schedules.json').read_bytes(),before)
        self.assertEqual(read_registration_status(self.base)['status'],'failed')

    def test_corrupt_registration_receipt_blocks_bootstrap_but_explicit_sync_can_repair(self):
        from schedule_settings import ScheduleSettingsError
        load_settings(self.base,create=True)
        receipt=self.base/'data/scheduler/task_registration.json';receipt.write_text('invalid receipt')
        with self.assertRaises(ScheduleSettingsError):
            wt.run_windows_action('bootstrap',self.base,runner=self.intercepted,platform_name='nt')
        self.assertEqual(self.calls,[])
        self.assertEqual(receipt.read_text(),'invalid receipt')
        self.assertEqual(wt.run_windows_action('sync',self.base,runner=self.intercepted,platform_name='nt'),0)
        self.assertEqual(read_registration_status(self.base)['status'],'registered')

    def test_password_mode_dirty_bootstrap_never_opens_an_unattended_prompt(self):
        wt.run_windows_action('sync',self.base,logon_mode='Password',runner=self.intercepted,platform_name='nt')
        save_schedule(self.base,dict(get_schedule(self.base,'month-end'),time='18:00'))
        self.calls.clear()
        self.assertEqual(wt.run_windows_action('bootstrap',self.base,runner=self.intercepted,platform_name='nt'),1)
        self.assertEqual(self.calls,[])
        self.assertEqual(read_registration_status(self.base)['status'],'needs-registration')

    def test_existing_v15_password_task_bootstrap_keeps_mode_and_requires_explicit_sync(self):
        runner=Mock(return_value=Mock(returncode=wt.PASSWORD_REGISTRATION_REQUIRED))
        self.assertEqual(wt.run_windows_action('bootstrap',self.base,runner=runner,platform_name='nt'),12)
        self.assertEqual(runner.call_args.args[0][runner.call_args.args[0].index('-Action')+1],'bootstrap')
        state=read_registration_status(self.base)
        self.assertEqual((state['status'],state['logon_mode']),('needs-registration','Password'))
        runner.reset_mock()
        self.assertEqual(wt.run_windows_action('bootstrap',self.base,runner=runner,platform_name='nt'),1)
        runner.assert_not_called()
        source=wt.PS_SOURCE
        self.assertLess(source.index('Principal.LogonType -eq 1'),source.index("$phase='タスク登録'"))
        self.assertIn('exit 12',source)

    def test_cache_identity_uses_actual_windows_sid_and_fails_closed(self):
        with patch.object(wt.os,'name','nt'),patch.object(wt,'_windows_sid',return_value='S-1-5-21-1001'):
            first=wt._identity_key()
        with patch.object(wt.os,'name','nt'),patch.object(wt,'_windows_sid',return_value='S-1-5-21-1002'):
            self.assertNotEqual(first,wt._identity_key())
        with patch.object(wt.os,'name','nt'),patch.object(wt,'_windows_sid',side_effect=OSError('synthetic SID failure')):
            with self.assertRaises(OSError):wt._identity_key()

    def test_native_registration_lock_blocks_bootstrap_sync_overlap_before_any_process(self):
        runner=Mock(return_value=Mock(returncode=0))
        with wt._registration_lock(self.base):
            with self.assertRaises(wt.RegistrationBusy):
                wt.run_windows_action('sync',self.base,runner=runner,platform_name='nt')
        runner.assert_not_called()
        self.assertIsNone(read_registration_status(self.base))

    def test_registration_lock_spans_native_operation_and_releases_after_launch_failure(self):
        def overlap(command,**options):
            with self.assertRaises(wt.RegistrationBusy):
                wt.run_windows_action('sync',self.base,runner=Mock(),platform_name='nt')
            return Mock(returncode=0)
        self.assertEqual(wt.run_windows_action('bootstrap',self.base,runner=overlap,platform_name='nt'),0)
        self.assertEqual(read_registration_status(self.base)['status'],'registered')
        with self.assertRaises(OSError):
            wt.run_windows_action('sync',self.base,runner=Mock(side_effect=OSError('synthetic launch failure')),platform_name='nt')
        self.assertEqual(wt.run_windows_action('sync',self.base,runner=self.intercepted,platform_name='nt'),0)

    def test_uninstall_tombstones_persist_even_if_native_removal_fails(self):
        self.assertEqual(wt.run_windows_action('uninstall',self.base,
            runner=Mock(return_value=Mock(returncode=9)),platform_name='nt'),9)
        self.assertTrue(all(item['deleted'] and not item['enabled'] for item in load_settings(self.base)['schedules']))

    def test_password_value_is_not_a_json_property_or_command_argument(self):
        wt.run_windows_action('sync',self.base,logon_mode='Password',runner=self.intercepted,platform_name='nt')
        command,_,_,payload=self.calls[0]
        self.assertNotIn('-Password',command)
        self.assertEqual(command[-2:],['-LogonMode','Password'])
        self.assertNotIn('password',json.dumps(payload).lower())

    def test_show_browser_and_pdf_are_explicit_test_flags_only(self):
        runner=Mock(return_value=Mock(returncode=0))
        wt.run_windows_action('test',self.base,runner=runner,platform_name='nt',show_browser=True,include_pdfs=True)
        command=runner.call_args.args[0]
        self.assertIn('--test',command);self.assertIn('--show-browser',command);self.assertIn('--include-pdfs',command)
        self.assertNotIn('--scheduled',command)

    def test_com_native_source_uses_read_data_and_scoped_owner_checks(self):
        source=wt.PS_SOURCE
        self.assertIn('ConvertFrom-Json',source)
        self.assertIn('Triggers.Create(1)',source)
        self.assertIn('Triggers.Create(4)',source)
        self.assertIn('$settings.Enabled=$item.Enabled',source)
        self.assertIn('$trigger.StartBoundary=$item.Boundary',source)
        self.assertIn('--schedule-id ',source)
        self.assertIn('--schedule-revision ',source)
        self.assertIn('$taskUser -ne $currentSid',source)
        self.assertIn('if($item.Deleted)',source)
        self.assertIn('if(-not $existing.ContainsKey($name))',source)
        self.assertNotIn('Invoke-Expression',source)


if __name__=='__main__': unittest.main()
