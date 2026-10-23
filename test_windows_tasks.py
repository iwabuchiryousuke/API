"""Pure task specs and intercepted process tests. No Windows task is created."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import windows_tasks as wt


class WindowsTaskPlanning(unittest.TestCase):
    def test_two_native_monthly_tasks_have_exact_lastday_and_day1_masks(self):
        plan=wt.build_task_plan('/tmp/synthetic app',user_sid='S-1-5-21-1-2-3-1001')
        end,start=plan['tasks']
        self.assertTrue(end['run_on_last_day_of_month']);self.assertEqual(end['days_of_month'],0)
        self.assertFalse(start['run_on_last_day_of_month']);self.assertEqual(start['days_of_month'],1)
        self.assertEqual(end['trigger_type'],4);self.assertEqual(start['months_of_year'],4095)
        self.assertEqual(plan['time_zone_requirement'],'Tokyo Standard Time')

    def test_defaults_are_least_privilege_same_user_interactive_not_s4u(self):
        for task in wt.build_task_plan('/tmp/app')['tasks']:
            self.assertEqual(task['logon_mode'],'InteractiveToken');self.assertEqual(task['run_level'],0)
            self.assertTrue(task['wake_to_run']);self.assertTrue(task['start_when_available'])
            self.assertEqual(task['multiple_instances'],'IgnoreNew')
            self.assertEqual((task['restart_interval'],task['restart_count']),('PT15M',3))
            self.assertEqual(task['execution_time_limit'],'PT2H')
            self.assertFalse(task['disallow_start_if_on_batteries']);self.assertFalse(task['stop_if_going_on_batteries'])

    def test_tasks_have_distinct_stable_per_user_and_per_app_names(self):
        def names(base,sid):return [t['name'] for t in wt.build_task_plan(base,user_sid=sid)['tasks']]
        a=names('/tmp/app','S-1-5-21-1');self.assertNotEqual(a[0],a[1])
        self.assertEqual(a,names('/tmp/app','S-1-5-21-1'))
        self.assertNotEqual(a,names('/tmp/other','S-1-5-21-1'))
        self.assertNotEqual(a,names('/tmp/app','S-1-5-21-2'))

    def test_task_arguments_are_schedule_flag_not_test_or_credentials(self):
        for task in wt.build_task_plan('/tmp/family app 日本語')['tasks']:
            self.assertEqual(task['arguments'],['monthly_runner.py','--role',task['role'],'--scheduled'])
            self.assertNotIn('--test',task['arguments'])

    def test_password_plan_is_metadata_only(self):
        plan=wt.build_task_plan('/tmp/app',logon_mode='Password')
        self.assertEqual(plan['tasks'][0]['logon_mode'],'Password')
        self.assertNotIn('password',set(plan['tasks'][0]))

    def test_bad_logon_or_sid_rejected(self):
        with self.assertRaises(ValueError):wt.build_task_plan('/tmp/app',logon_mode='S4U')
        with self.assertRaises(ValueError):wt.build_task_plan('/tmp/app',user_sid='other-user')

    def test_filesystem_root_is_not_trimmed_to_relative_drive_syntax(self):
        root=Path(Path(wt.__file__).anchor)
        plan=wt.build_task_plan(root)
        self.assertEqual(plan['tasks'][0]['working_directory'],str(root.resolve()))
        self.assertIn('$AppRoot.Length -gt [IO.Path]::GetPathRoot($AppRoot).Length',wt.PS_SOURCE)

    def test_dry_run_on_linux_is_read_only_and_does_not_launch_powershell(self):
        runner=Mock()
        with redirect_stdout(io.StringIO()) as output:
            result=wt.run_windows_action('dry-run',base='/tmp/app',runner=runner,platform_name='posix')
        self.assertEqual(result,0);runner.assert_not_called()
        self.assertIn('"planning_only": true',output.getvalue())

    def test_real_actions_are_refused_on_non_windows(self):
        with self.assertRaises(RuntimeError):wt.run_windows_action('install',base='/tmp/app',platform_name='posix')

    def test_embedded_ps_is_written_with_bom_and_removed_after_process(self):
        seen=[]
        with tempfile.TemporaryDirectory() as base:
            def intercepted(command,**kwargs):
                script=Path(command[command.index('-File')+1]);seen.append(script)
                self.assertTrue(script.read_bytes().startswith(b'\xef\xbb\xbf'))
                self.assertIn('RunOnLastDayOfMonth',script.read_text('utf-8-sig'))
                self.assertEqual(command[command.index('-ExecutionPolicy')+1],'Bypass')
                self.assertEqual(command[command.index('-AppRoot')+1],str(Path(base).resolve()))
                self.assertFalse(kwargs.get('shell',False));self.assertFalse(kwargs['check'])
                return Mock(returncode=0)
            self.assertEqual(wt.run_windows_action('install',base=base,runner=intercepted,platform_name='nt'),0)
        self.assertTrue(seen);self.assertFalse(seen[0].exists())

    def test_temp_script_removed_when_process_launch_fails(self):
        seen=[]
        def fail(command,**kwargs):
            seen.append(Path(command[command.index('-File')+1]));raise OSError('injected process launch failure')
        with self.assertRaises(OSError):wt.run_windows_action('status',base='/tmp/app',runner=fail,platform_name='nt')
        self.assertFalse(seen[0].exists())

    def test_child_exit_code_is_preserved(self):
        self.assertEqual(wt.run_windows_action('install',base='/tmp/app',runner=Mock(return_value=Mock(returncode=7)),platform_name='nt'),7)

    def test_password_value_is_never_in_command_line(self):
        seen=[]
        def intercepted(command,**kwargs):seen.extend(command);return Mock(returncode=0)
        wt.run_windows_action('install',base='/tmp/app',logon_mode='Password',runner=intercepted,platform_name='nt')
        self.assertEqual(seen[-2:],['-LogonMode','Password'])
        self.assertNotIn('-Password',seen);self.assertNotIn('-Credential',seen)

    def test_manual_test_calls_worker_test_without_scheduled_or_pdf_flags(self):
        runner=Mock(return_value=Mock(returncode=0))
        wt.run_windows_action('test',base='/tmp/app',role='month-start',runner=runner,platform_name='nt')
        command=runner.call_args.args[0]
        self.assertEqual(command[-3:],['--role','month-start','--test'])
        self.assertNotIn('--scheduled',command);self.assertNotIn('--include-pdfs',command)

    def test_com_source_uses_native_lastday_and_all_required_settings(self):
        source=wt.PS_SOURCE
        for expression in ['Triggers.Create(4)','RunOnLastDayOfMonth=$item.Last','MonthsOfYear=4095',
                           'Principal.RunLevel=0','WakeToRun=$true','StartWhenAvailable=$true',
                           "RestartInterval='PT15M'",'RestartCount=3',"ExecutionTimeLimit='PT2H'",'MultipleInstances=2']:
            self.assertIn(expression,source)
        self.assertNotIn('<Last>',source);self.assertNotIn('Set-ExecutionPolicy',source)
        self.assertNotIn('Set-TimeZone',source);self.assertNotIn('LogonType=2',source)

    def test_com_credentials_only_inside_password_branch_same_sid_check(self):
        source=wt.PS_SOURCE
        self.assertIn("if($LogonMode -eq 'Password')",source)
        self.assertIn('Get-Credential -UserName $currentName',source)
        self.assertIn('$credentialSid -ne $currentSid',source)
        self.assertIn('$password=$credential.GetNetworkCredential().Password',source)
        self.assertIn('$currentSid,$password,$logonType,$null',source)
        self.assertIn('$credential.Password.Dispose()',source)
        self.assertNotIn('ConvertFrom-SecureString',source)
        self.assertNotIn('Export-Clixml',source)

    def test_status_and_uninstall_are_ownership_scoped_and_do_not_require_timezone(self):
        source=wt.PS_SOURCE
        self.assertIn('$task.Definition.RegistrationInfo.Description -ne $marker',source)
        self.assertIn('$taskUser -ne $currentSid',source)
        self.assertLess(source.index("if($Action -eq 'uninstall')"),source.index("if([TimeZoneInfo]::Local.Id"))
        self.assertIn('$folder.DeleteTask($name,0)',source)
        self.assertNotIn('GetTasks(',source)

    def test_batch_wrappers_preserve_exit_and_unattended_worker_has_no_pause(self):
        base=Path(wt.__file__).parent
        for name in ['setup_monthly_tasks.bat','monthly_tasks_status.bat','remove_monthly_tasks.bat','show_monthly_tasks_plan.bat','test_monthly_tasks.bat']:
            data=(base/name).read_bytes();self.assertIn(b'\r\n',data)
            self.assertIn(b'exit /b %MONTHLY_TASK_RESULT%',data)
            self.assertNotIn(b'Get-Credential',data)
        auto=(base/'auto_run_monthly.bat').read_text()
        self.assertNotIn('pause',auto.lower())
        self.assertIn('--scheduled',auto);self.assertIn('exit /b %MONTHLY_RUN_RESULT%',auto)


if __name__=='__main__':unittest.main()
