"""Manage per-user monthly and one-time tasks with a generated COM script.

The PowerShell source is embedded so older code-only updaters can install this
module and its .bat wrappers without knowing a new .ps1 file extension.
Passwords are requested by Get-Credential and passed to COM in memory only.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from schedule_settings import (JST, ScheduleSettingsError, defaults, load_settings,
                               settings_digest, validate_settings)


TASK_PREFIX='SBI_FamilyTrades'
PASSWORD_REGISTRATION_REQUIRED=12


class RegistrationBusy(ScheduleSettingsError):
    pass


@contextmanager
def _registration_lock(base):
    """Serialize automatic bootstrap and explicit native registration updates."""
    from schedule_settings import _folder, _regular
    folder=_folder(base,create=True)
    path=folder/'.task_registration.lock'
    if path.exists() or path.is_symlink():_regular(path)
    descriptor=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_BINARY',0)|
                       getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        with os.fdopen(descriptor,'a+b',closefd=False) as stream:
            _regular(path)
            stream.seek(0)
            if not stream.read(1):stream.write(b'0');stream.flush()
            stream.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                raise RegistrationBusy('別の予約反映が実行中です。終了後にもう一度反映してください。') from None
            yield
    finally:
        os.close(descriptor)


def build_task_plan(base,python_executable=None,user_sid='CURRENT_USER_SID',logon_mode='InteractiveToken'):
    """Pure preview of fixed settings; this never registers or starts a task."""
    if logon_mode not in ('InteractiveToken','Password'):raise ValueError('対応するログオン方式ではありません。')
    if user_sid!='CURRENT_USER_SID' and not re.fullmatch(r'S-\d+(?:-\d+)+',user_sid):raise ValueError('WindowsユーザーSIDを確認してください。')
    root=str(Path(base).resolve())
    fingerprint=hashlib.sha256(root.encode('utf-8')).hexdigest()[:12]
    prefix=TASK_PREFIX+'_'+user_sid.replace('-','_')+'_'+fingerprint
    # Resolving a venv's interpreter symlink can discard that environment.
    interpreter=str(Path(python_executable or sys.executable).absolute())
    tasks=[]
    for role,suffix,last,days in [('month-end','MonthEnd16JST',True,0),('month-start','MonthStart16JST',False,1)]:
        tasks.append({'name':prefix+'_'+suffix,'role':role,'trigger_type':4,
            'run_on_last_day_of_month':last,'days_of_month':days,'months_of_year':4095,
            'start_boundary_time':'16:00:00+09:00','python':interpreter,
            'arguments':['monthly_runner.py','--role',role,'--scheduled'],
            'working_directory':root,'logon_mode':logon_mode,'run_level':0,
            'wake_to_run':True,'start_when_available':True,'multiple_instances':'IgnoreNew',
            'restart_interval':'PT15M','restart_count':3,'execution_time_limit':'PT2H',
            'disallow_start_if_on_batteries':False,'stop_if_going_on_batteries':False})
    return {'planning_only':True,'time_zone_requirement':'Tokyo Standard Time','tasks':tasks,
            'notes':['InteractiveTokenは同じWindowsユーザーのログオンが必要です。',
                     'Password方式は同じユーザーのWindowsパスワードを安全な入力欄で指定します。',
                     '電源OFF中は動きません。スリープ解除はWindows・機器の電源設定にも依存します。',
                     '遅延起動でも別の暦日を過去の月末・月初として記録しません。']}


def _task_suffix(identifier):
    return {'month-end': 'MonthEnd16JST', 'month-start': 'MonthStart16JST'}.get(
        identifier, 'Once_' + identifier)


def build_configured_plan(base, settings=None, python_executable=None,
                          user_sid='CURRENT_USER_SID', logon_mode='InteractiveToken', now=None):
    """Pure native task definitions for saved schedules, including tombstones."""
    template = build_task_plan(base, python_executable, user_sid, logon_mode)
    if settings is None:
        settings = load_settings(base) if Path(base).is_dir() else defaults()
    settings = validate_settings(settings)
    stamp = now or datetime.now(JST)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('タイムゾーン付きの現在時刻が必要です。')
    stamp = stamp.astimezone(JST)
    prefix = template['tasks'][0]['name'].removesuffix('_MonthEnd16JST')
    result = []
    for entry in settings['schedules']:
        suffix = _task_suffix(entry['id'])
        boundary = (entry['run_at'] if entry['kind'] == 'once' else
                    stamp.strftime('%Y-%m-%d') + 'T' + entry['time'] + ':00+09:00')
        task = dict(template['tasks'][0])
        task.update(name=prefix + '_' + suffix, schedule_id=entry['id'],
                    schedule_revision=entry['revision'], label=entry['label'],
                    role=entry['kind'], trigger_type=1 if entry['kind'] == 'once' else 4,
                    run_on_last_day_of_month=entry['kind'] == 'month-end',
                    days_of_month=1 if entry['kind'] == 'month-start' else 0,
                    months_of_year=0 if entry['kind']=='once' else 4095,
                    start_boundary=boundary, start_boundary_time=entry['time']+':00+09:00',
                    enabled=entry['enabled'], deleted=entry['deleted'],
                    include_pdfs=entry['include_pdfs'],
                    arguments=['monthly_runner.py', '--schedule-id', entry['id'],
                               '--schedule-revision', str(entry['revision']), '--scheduled'])
        result.append(task)
    return dict(template, tasks=result, settings_revision=settings['revision'],
                settings_digest=settings_digest(settings))


def _registration_payload(plan):
    return dict(schema='sbi-task-registration-plan', version=1, tasks=[
        dict(Id=task['schedule_id'], Revision=task['schedule_revision'],
             Role=task['role'], Suffix=_task_suffix(task['schedule_id']),
             Last=task['run_on_last_day_of_month'], Days=task['days_of_month'],
             Boundary=task['start_boundary'], Enabled=task['enabled'], Deleted=task['deleted'])
        for task in plan['tasks']])


def _windows_sid():
    """Read the current process SID without a shell or credential prompt."""
    import ctypes
    from ctypes import wintypes
    advapi=ctypes.WinDLL('advapi32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    advapi.OpenProcessToken.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype=wintypes.BOOL
    advapi.GetTokenInformation.argtypes=[wintypes.HANDLE,ctypes.c_uint,wintypes.LPVOID,
                                        wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype=wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes=[wintypes.LPVOID,ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.LocalFree.argtypes=[wintypes.HLOCAL]
    kernel.LocalFree.restype=wintypes.HLOCAL
    token=wintypes.HANDLE();needed=wintypes.DWORD();output=wintypes.LPWSTR()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(),8,ctypes.byref(token)):
        raise OSError('WindowsユーザーSIDを確認できません。')
    try:
        advapi.GetTokenInformation(token,1,None,0,ctypes.byref(needed))
        if not needed.value or needed.value>65536:
            raise OSError('WindowsユーザーSIDの長さを確認できません。')
        buffer=ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token,1,buffer,needed,ctypes.byref(needed)):
            raise OSError('WindowsユーザーSIDを確認できません。')
        # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first field is PSID.
        sid=ctypes.cast(buffer,ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid,ctypes.byref(output)):
            raise OSError('WindowsユーザーSIDを文字列に変換できません。')
        result=output.value
        if not result or not re.fullmatch(r'S-\d+(?:-\d+)+',result):
            raise OSError('WindowsユーザーSIDの形式を確認できません。')
        return result
    finally:
        if output:kernel.LocalFree(ctypes.cast(output,wintypes.HLOCAL))
        kernel.CloseHandle(token)


def _identity_key():
    # This is only a per-profile cache key. COM independently checks the real
    # current SID again before touching a Windows task.
    identity=(_windows_sid() if os.name=='nt' else
              os.environ.get('USERDOMAIN','')+'\\'+getpass.getuser())
    return hashlib.sha256(identity.casefold().encode('utf-8')).hexdigest()


def _registration_state(base):
    from schedule_settings import _folder, _read
    path = _folder(base) / 'task_registration.json'
    if not path.exists() and not path.is_symlink():
        return None
    value = _read(path)
    required = {'schema', 'version', 'status', 'settings_revision', 'settings_digest',
                'logon_mode', 'updated_at', 'app_root', 'identity_key'}
    if (not isinstance(value, dict) or set(value) != required or
            value.get('schema') != 'sbi-task-registration' or type(value.get('version')) is not int or value.get('version') != 1 or
            value.get('status') not in ('registered', 'failed', 'needs-registration') or
            value.get('logon_mode') not in ('InteractiveToken', 'Password') or
            type(value.get('settings_revision')) is not int or value.get('settings_revision',0)<1 or
            not isinstance(value.get('settings_digest'),str) or not re.fullmatch(r'[a-f0-9]{64}',value['settings_digest']) or
            not isinstance(value.get('identity_key'),str) or not re.fullmatch(r'[a-f0-9]{64}',value['identity_key']) or
            not isinstance(value.get('app_root'),str)):
        raise ScheduleSettingsError('Windows反映状況の形式を確認できません。既存記録を保持しました。')
    return value


def _save_registration(base, settings, logon_mode, status):
    from schedule_settings import _folder, _write
    path = _folder(base, create=True) / 'task_registration.json'
    value = dict(schema='sbi-task-registration', version=1, status=status,
                 settings_revision=settings['revision'], settings_digest=settings_digest(settings),
                 logon_mode=logon_mode, updated_at=datetime.now(JST).isoformat(),
                 app_root=str(Path(base).resolve()), identity_key=_identity_key())
    _write(path, value)


PS_SOURCE=r'''param(
 [Parameter(Mandatory=$true)][string]$AppRoot,
 [Parameter(Mandatory=$true)][string]$PythonExe,
 [ValidateSet('install','status','uninstall','dry-run','bootstrap')][string]$Action='dry-run',
 [ValidateSet('InteractiveToken','Password')][string]$LogonMode='InteractiveToken',
 [Parameter(Mandatory=$true)][string]$ConfigFile
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$OutputEncoding=New-Object -TypeName 'System.Text.UTF8Encoding' -ArgumentList $false
[Console]::OutputEncoding=$OutputEncoding
$credential=$null
$password=$null
$sha=$null
$installed=@()
$phase='準備'
function Test-TaskMissing($record) {
 $errorObject=$record.Exception
 while($null -ne $errorObject) {
  if($errorObject.HResult -eq -2147024894 -or $errorObject.HResult -eq -2147216625) { return $true }
  $errorObject=$errorObject.InnerException
 }
 return $false
}
try {
 $AppRoot=[IO.Path]::GetFullPath($AppRoot)
 if($AppRoot.Length -gt [IO.Path]::GetPathRoot($AppRoot).Length) {
  $AppRoot=$AppRoot.TrimEnd([char[]]@('\','/'))
 }
 $PythonExe=[IO.Path]::GetFullPath($PythonExe)
 $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
 $currentName=$identity.Name
 $currentSid=$identity.User.Value
 if($currentSid -in @('S-1-5-18','S-1-5-19','S-1-5-20')) { throw '通常のWindowsユーザーで実行してください。' }
 $sha=[Security.Cryptography.SHA256]::Create()
 $hash=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($AppRoot))).Replace('-','').ToLowerInvariant().Substring(0,12)
 $prefix='SBI_FamilyTrades_'+$currentSid.Replace('-','_')+'_'+$hash
 $payload=Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
 if($payload.schema -ne 'sbi-task-registration-plan' -or $payload.version -ne 1) { throw '予約設定を確認できません。' }
 $roles=@($payload.tasks)
 if($roles.Count -gt 500) { throw '予約件数を確認できません。' }
 foreach($item in $roles) {
  if($item.Id -notmatch '^[a-z][a-z0-9_-]{0,63}$' -or $item.Suffix -notmatch '^[A-Za-z0-9_-]{1,80}$' -or $item.Role -notin @('month-end','month-start','once') -or $item.Revision -lt 1 -or $item.Revision -gt 2147483647) { throw '予約IDを確認できません。' }
  if($item.Boundary -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00\+09:00$') { throw '予約日時を確認できません。' }
  [void][DateTimeOffset]::Parse($item.Boundary,[Globalization.CultureInfo]::InvariantCulture)
  if($item.Enabled -isnot [bool] -or $item.Deleted -isnot [bool]) { throw '予約状態を確認できません。' }
 }
 if($Action -eq 'dry-run') {
  foreach($item in $roles) {
  Write-Output ('予定：'+$prefix+'_'+$item.Suffix+' / '+$item.Role+' / '+$item.Boundary+' / '+$LogonMode)
  }
  Write-Output '確認表示のみです。タスク登録・SBI接続・データ変更は行っていません。'
  exit 0
 }
 $phase='タスクスケジューラへの接続'
 $service=New-Object -ComObject 'Schedule.Service'
 $service.Connect()
 $folder=$service.GetFolder('\')
 $existing=@{}
 foreach($item in $roles) {
  $name=$prefix+'_'+$item.Suffix
  $task=$null
  try { $task=$folder.GetTask($name) } catch {
   # Only the documented not-found HRESULTs mean the task is absent.
   if(-not (Test-TaskMissing $_)) { throw }
  }
  if($null -ne $task) {
   $marker='SBI Family Trades acquisition; app='+$AppRoot+'; schedule='+$item.Id
   $legacyMarker='SBI Family Trades monthly; app='+$AppRoot+'; role='+$item.Role
   $ownedDescription=$task.Definition.RegistrationInfo.Description -eq $marker
   if($item.Id -in @('month-end','month-start') -and $task.Definition.RegistrationInfo.Description -eq $legacyMarker) { $ownedDescription=$true }
   $taskUser=$task.Definition.Principal.UserId
   if((-not $ownedDescription) -or ($taskUser -ne $currentSid -and $taskUser -ne $currentName)) {
    throw '同名の別タスクがあるため変更できません。'
   }
   $existing[$name]=$task
  }
 }
 if($Action -eq 'bootstrap') {
  foreach($task in $existing.Values) {
   if($task.Definition.Principal.LogonType -eq 1) {
    Write-Output '既存のパスワード方式の予約を保持しました。設定画面から同じ方式で明示的に反映してください。自動でログオン中方式へ変更しません。'
    exit 12
   }
  }
 }
 if($Action -eq 'status') {
  foreach($item in $roles) {
   $name=$prefix+'_'+$item.Suffix
   if($existing.ContainsKey($name)) {
    $task=$existing[$name]
    [pscustomobject]@{Name=$name;Role=$item.Role;Enabled=$task.Enabled;State=$task.State;
     LastRunTime=$task.LastRunTime;LastTaskResult=$task.LastTaskResult;NextRunTime=$task.NextRunTime;
     LogonType=$task.Definition.Principal.LogonType;User=$currentName} | Format-List
   } else { Write-Output ('未登録：'+$name) }
  }
  Write-Output '結果コード0だけではSBI画面での取得成功を保証しません。アプリの自動取得履歴も確認してください。'
  exit 0
 }
 if($Action -eq 'uninstall') {
  foreach($item in $roles) {
   $name=$prefix+'_'+$item.Suffix
   if($existing.ContainsKey($name)) { $folder.DeleteTask($name,0);Write-Output ('登録解除：'+$name) }
   else { Write-Output ('未登録：'+$name) }
  }
  Write-Output 'タスク登録だけを削除しました。データ・認証設定・取得履歴は削除していません。実行中の処理は別途終了を確認してください。'
  exit 0
 }
 $phase='設定条件の確認'
 if([TimeZoneInfo]::Local.Id -ne 'Tokyo Standard Time') {
  throw 'Windowsのタイムゾーンを「(UTC+09:00) 大阪、札幌、東京」に手動設定してから再実行してください。自動変更は行いません。'
 }
 if(-not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $AppRoot 'monthly_runner.py') -PathType Leaf)) {
  throw 'Pythonまたはmonthly_runner.pyが見つかりません。最新版のコードを確認してください。'
 }
 if($LogonMode -eq 'Password') {
  $phase='同じWindowsユーザーの資格情報確認'
  $credential=Get-Credential -UserName $currentName -Message '同じWindowsユーザーのパスワードを入力してください。PINではありません。値をファイル・引数・ログには保存しません。'
  if($null -eq $credential) { throw '資格情報の入力を中止しました。' }
  $nt=New-Object -TypeName 'System.Security.Principal.NTAccount' -ArgumentList $credential.UserName
  $credentialSid=$nt.Translate([Security.Principal.SecurityIdentifier]).Value
  if($credentialSid -ne $currentSid) { throw 'アプリの暗号化設定を使用する同じWindowsユーザーだけを指定できます。' }
  $password=$credential.GetNetworkCredential().Password
  if([string]::IsNullOrEmpty($password)) { throw '空のWindowsパスワードでは登録しません。' }
 }
 $zone=[TimeZoneInfo]::FindSystemTimeZoneById('Tokyo Standard Time')
 $jstNow=[TimeZoneInfo]::ConvertTime([DateTimeOffset]::Now,$zone)
 $definitions=@()
 foreach($item in $roles) {
  if($item.Deleted) { continue }
  $definition=$service.NewTask(0)
  $definition.RegistrationInfo.Author=$currentName
  $definition.RegistrationInfo.Description='SBI Family Trades acquisition; app='+$AppRoot+'; schedule='+$item.Id
  $definition.Principal.UserId=$currentSid
  $definition.Principal.LogonType=3
  if($LogonMode -eq 'Password') { $definition.Principal.LogonType=1 }
  $definition.Principal.RunLevel=0
  $settings=$definition.Settings
  $settings.Enabled=$item.Enabled
  $settings.AllowDemandStart=$true
  $settings.WakeToRun=$true
  $settings.StartWhenAvailable=$true
  $settings.DisallowStartIfOnBatteries=$false
  $settings.StopIfGoingOnBatteries=$false
  $settings.RunOnlyIfIdle=$false
  $settings.MultipleInstances=2
  $settings.RestartInterval='PT15M'
  $settings.RestartCount=3
  $settings.ExecutionTimeLimit='PT2H'
  if($item.Role -eq 'once') {
   $trigger=$definition.Triggers.Create(1)
  } else {
   $trigger=$definition.Triggers.Create(4)
   $trigger.MonthsOfYear=4095
   $trigger.RunOnLastDayOfMonth=$item.Last
   $trigger.DaysOfMonth=$item.Days
  }
  $trigger.Id=$item.Id
  $trigger.StartBoundary=$item.Boundary
  $trigger.Enabled=$item.Enabled
  $exec=$definition.Actions.Create(0)
  $exec.Path=$PythonExe
  $runner=Join-Path $AppRoot 'monthly_runner.py'
  $exec.Arguments='"'+$runner+'" --schedule-id '+$item.Id+' --schedule-revision '+$item.Revision+' --scheduled'
  $exec.WorkingDirectory=$AppRoot
  $definitions+=@{Name=$prefix+'_'+$item.Suffix;Definition=$definition}
 }
 $phase='タスク登録'
 foreach($entry in $definitions) {
  $logonType=3
  if($LogonMode -eq 'Password') { $logonType=1 }
  [void]$folder.RegisterTaskDefinition($entry.Name,$entry.Definition,6,$currentSid,$password,$logonType,$null)
  $installed+=$entry.Name
  Write-Output ('登録：'+$entry.Name)
 }
 foreach($item in $roles) {
  if($item.Deleted) {
   $name=$prefix+'_'+$item.Suffix
   if($existing.ContainsKey($name)) { $folder.DeleteTask($name,0);Write-Output ('削除済み予約を解除：'+$name) }
  }
 }
 Write-Output '保存した予約をWindowsへ反映しました。WakeToRunを指定していますが、PC電源OFF・電源設定・追加認証などで実行できない場合があります。'
 if($LogonMode -eq 'InteractiveToken') { Write-Output '同じWindowsユーザーにログオンした状態で使用してください。サインアウト中は実行できません。画面ロックとサインアウトは異なります。' }
 else { Write-Output 'Windowsはタスク用資格情報を管理します。Windowsパスワードを変更したら再登録してください。Windows Hello/PINだけの環境ではこの方式を使用できない場合があります。' }
 Write-Output 'アプリの自動取得履歴で、実際の取得時刻・遅延・不足項目を確認してください。16:00ちょうどのSBI評価額を保証するものではありません。'
 exit 0
} catch {
 # Never echo a credential, full exception or COM arguments.
 Write-Output ('完了できませんでした：'+$phase+'。')
 if($phase -eq '設定条件の確認') {
  Write-Output 'Windowsのタイムゾーンを「(UTC+09:00) 大阪、札幌、東京」に設定し、Pythonとmonthly_runner.pyの場所を確認してください。'
 } elseif($phase -eq '同じWindowsユーザーの資格情報確認') {
  Write-Output '同じWindowsユーザーのパスワード（PINではありません）と、タスクとしてログオンする権限を確認してください。'
 } else {
  Write-Output 'Windowsのタスクスケジューラ・組織の実行ポリシー・権限を確認し、状態確認から再実行してください。'
 }
 if($installed.Count -gt 0) {
  foreach($name in $installed) {
   if(-not $existing.ContainsKey($name)) { try { $folder.DeleteTask($name,0) } catch {} }
  }
  Write-Output '新規作成分の解除を試みました。既存予約の更新が一部だけ反映された可能性があるため、登録状態を確認してください。'
 }
 exit 1
} finally {
 $password=$null
 if($null -ne $credential) { $credential.Password.Dispose() }
 $credential=$null
 if($null -ne $sha) { $sha.Dispose() }
}
'''


def _powershell_command(script,base,action,logon_mode,config_file):
    system_root=os.environ.get('SystemRoot',r'C:\Windows')
    shell=Path(system_root)/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
    return [str(shell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
            '-AppRoot',str(base),'-PythonExe',sys.executable,'-Action',action,
            '-ConfigFile',str(config_file),'-LogonMode',logon_mode]


def _run_windows_action(action,base=None,logon_mode='InteractiveToken',role='month-end',
                       runner=subprocess.run,platform_name=None,show_browser=False,include_pdfs=False):
    root=Path(base or Path(__file__).resolve().parent).resolve()
    if logon_mode not in ('InteractiveToken','Password'):raise ValueError('対応するログオン方式ではありません。')
    if action=='dry-run':
        print(json.dumps(build_configured_plan(root,logon_mode=logon_mode),ensure_ascii=False,indent=2));return 0
    if action not in ('install','status','uninstall','test','sync','bootstrap'):raise ValueError('対応する操作ではありません。')
    if (platform_name or os.name)!='nt':raise RuntimeError('Windows上の、SBI・Gmailを設定した同じユーザーで実行してください。')
    if role not in ('month-end','month-start'):raise ValueError('roleはmonth-end/month-startです。')
    if action=='test':
        command=[sys.executable,str(root/'monthly_runner.py'),'--role',role,'--test']
        if show_browser:command.append('--show-browser')
        if include_pdfs:command.append('--include-pdfs')
        return runner(command,cwd=str(root),check=False).returncode
    persistent=root.is_dir()
    settings=load_settings(root,create=action in ('install','sync','bootstrap','uninstall')) if persistent else defaults()
    if action=='uninstall' and persistent:
        from schedule_settings import delete_schedule
        # The user's deletion intent must survive a native-task removal error.
        for item in settings['schedules']:
            if not item['deleted']:delete_schedule(root,item['id'])
        settings=load_settings(root)
    # A corrupt auxiliary receipt blocks unattended changes. An explicit sync
    # may repair it only after Windows has successfully applied valid settings.
    prior=_registration_state(root) if persistent and action=='bootstrap' else None
    if action=='bootstrap':
        same=(prior and prior['settings_digest']==settings_digest(settings) and
              prior['app_root']==str(root) and prior['identity_key']==_identity_key())
        if same and prior['status']=='registered':
            print('保存した予約はWindowsへ反映済みです。削除済み予約は復元しません。');return 0
        if prior and prior['logon_mode']=='Password':
            _save_registration(root,settings,'Password','needs-registration')
            print('パスワード方式の再登録が必要です。予約画面から明示的に反映してください。');return 1
        logon_mode='InteractiveToken'  # Never opens a credential prompt at app startup.
    native_action='install' if action=='sync' else action
    plan=build_configured_plan(root,settings=settings,logon_mode=logon_mode)
    temporary=None;configuration=None
    try:
        # UTF-8 BOM makes Japanese source readable by Windows PowerShell 5.1.
        with tempfile.NamedTemporaryFile(prefix='family_monthly_tasks_',suffix='.ps1',delete=False) as stream:
            temporary=Path(stream.name);stream.write(PS_SOURCE.encode('utf-8-sig'));stream.flush();os.fsync(stream.fileno())
        with tempfile.NamedTemporaryFile(prefix='family_acquisition_plan_',suffix='.json',delete=False) as stream:
            configuration=Path(stream.name)
            stream.write(json.dumps(_registration_payload(plan),ensure_ascii=False).encode('utf-8'))
            stream.flush();os.fsync(stream.fileno())
        code=runner(_powershell_command(temporary,root,native_action,logon_mode,configuration),cwd=str(root),check=False).returncode
        if persistent and action!='status':
            if action=='bootstrap' and code==PASSWORD_REGISTRATION_REQUIRED:
                _save_registration(root,settings,'Password','needs-registration')
            else:
                _save_registration(root,settings,logon_mode,'registered' if code==0 else 'failed')
        return code
    except OSError:
        if persistent and action!='status':_save_registration(root,settings,logon_mode,'failed')
        raise
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
        if configuration is not None:configuration.unlink(missing_ok=True)


def run_windows_action(action,base=None,logon_mode='InteractiveToken',role='month-end',
                       runner=subprocess.run,platform_name=None,show_browser=False,include_pdfs=False):
    root=Path(base or Path(__file__).resolve().parent).resolve()
    options=dict(base=root,logon_mode=logon_mode,role=role,runner=runner,
                 platform_name=platform_name,show_browser=show_browser,include_pdfs=include_pdfs)
    if (action in ('install','sync','bootstrap','uninstall') and
            (platform_name or os.name)=='nt' and root.is_dir()):
        with _registration_lock(root):
            return _run_windows_action(action,**options)
    return _run_windows_action(action,**options)


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser(description='SBI家族売買記録の月末・月初・指定日時のWindows予約。初期動作は確認表示だけです。')
    parser.add_argument('--action',choices=('dry-run','install','status','uninstall','test','sync','bootstrap'),default='dry-run')
    parser.add_argument('--logon-mode',choices=('InteractiveToken','Password'),default='InteractiveToken')
    parser.add_argument('--role',choices=('month-end','month-start'),default='month-end')
    parser.add_argument('--show-browser',action='store_true',help='明示テストでブラウザーを表示します。')
    parser.add_argument('--include-pdfs',action='store_true',help='明示テストでPDFも保存します。')
    args=parser.parse_args(argv)
    try:return run_windows_action(args.action,logon_mode=args.logon_mode,role=args.role,
                                  show_browser=args.show_browser,include_pdfs=args.include_pdfs)
    except RegistrationBusy as error:
        print(str(error),file=sys.stderr);return 1
    except (OSError,RuntimeError,ValueError):
        print('処理を完了できませんでした。Windowsの同じユーザー・Python・タスク実行権限を確認してください。',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
