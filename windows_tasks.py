"""Install two per-user Windows tasks using a generated local COM script.

The PowerShell source is embedded so older code-only updaters can install this
module and its .bat wrappers without knowing a new .ps1 file extension.
Passwords are requested by Get-Credential and passed to COM in memory only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


TASK_PREFIX='SBI_FamilyTrades'


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


PS_SOURCE=r'''param(
 [Parameter(Mandatory=$true)][string]$AppRoot,
 [Parameter(Mandatory=$true)][string]$PythonExe,
 [ValidateSet('install','status','uninstall','dry-run')][string]$Action='dry-run',
 [ValidateSet('InteractiveToken','Password')][string]$LogonMode='InteractiveToken'
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
 $roles=@(
  @{Role='month-end';Suffix='MonthEnd16JST';Last=$true;Days=0},
  @{Role='month-start';Suffix='MonthStart16JST';Last=$false;Days=1}
 )
 if($Action -eq 'dry-run') {
  foreach($item in $roles) {
   Write-Output ('予定：'+$prefix+'_'+$item.Suffix+' / '+$item.Role+' / 日本時間16:00 / '+$LogonMode)
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
   $marker='SBI Family Trades monthly; app='+$AppRoot+'; role='+$item.Role
   $taskUser=$task.Definition.Principal.UserId
   if($task.Definition.RegistrationInfo.Description -ne $marker -or ($taskUser -ne $currentSid -and $taskUser -ne $currentName)) {
    throw '同名の別タスクがあるため変更できません。'
   }
   $existing[$name]=$task
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
 $startBoundary=$jstNow.ToString('yyyy-MM-dd')+'T16:00:00+09:00'
 $definitions=@()
 foreach($item in $roles) {
  $definition=$service.NewTask(0)
  $definition.RegistrationInfo.Author=$currentName
  $definition.RegistrationInfo.Description='SBI Family Trades monthly; app='+$AppRoot+'; role='+$item.Role
  $definition.Principal.UserId=$currentSid
  $definition.Principal.LogonType=3
  if($LogonMode -eq 'Password') { $definition.Principal.LogonType=1 }
  $definition.Principal.RunLevel=0
  $settings=$definition.Settings
  $settings.Enabled=$true
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
  $trigger=$definition.Triggers.Create(4)
  $trigger.Id=$item.Suffix
  $trigger.MonthsOfYear=4095
  $trigger.RunOnLastDayOfMonth=$item.Last
  $trigger.DaysOfMonth=$item.Days
  $trigger.StartBoundary=$startBoundary
  $trigger.Enabled=$true
  $exec=$definition.Actions.Create(0)
  $exec.Path=$PythonExe
  $runner=Join-Path $AppRoot 'monthly_runner.py'
  $exec.Arguments='"'+$runner+'" --role '+$item.Role+' --scheduled'
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
 Write-Output '月末16:00と月初1日16:00（日本時間）の2タスクを登録しました。WakeToRunを指定していますが、PC電源OFF・電源設定・追加認証などで実行できない場合があります。'
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
  Write-Output '一部のタスクだけが登録された可能性があります。状態確認で2タスクを確認し、必要なら登録解除して再実行してください。'
 }
 exit 1
} finally {
 $password=$null
 if($null -ne $credential) { $credential.Password.Dispose() }
 $credential=$null
 if($null -ne $sha) { $sha.Dispose() }
}
'''


def _powershell_command(script,base,action,logon_mode):
    system_root=os.environ.get('SystemRoot',r'C:\Windows')
    shell=Path(system_root)/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
    return [str(shell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
            '-AppRoot',str(base),'-PythonExe',sys.executable,'-Action',action,'-LogonMode',logon_mode]


def run_windows_action(action,base=None,logon_mode='InteractiveToken',role='month-end',runner=subprocess.run,platform_name=None):
    root=Path(base or Path(__file__).resolve().parent).resolve()
    if logon_mode not in ('InteractiveToken','Password'):raise ValueError('対応するログオン方式ではありません。')
    if action=='dry-run':
        print(json.dumps(build_task_plan(root,logon_mode=logon_mode),ensure_ascii=False,indent=2));return 0
    if action not in ('install','status','uninstall','test'):raise ValueError('対応する操作ではありません。')
    if (platform_name or os.name)!='nt':raise RuntimeError('Windows上の、SBI・Gmailを設定した同じユーザーで実行してください。')
    if role not in ('month-end','month-start'):raise ValueError('roleはmonth-end/month-startです。')
    if action=='test':
        return runner([sys.executable,str(root/'monthly_runner.py'),'--role',role,'--test'],cwd=str(root),check=False).returncode
    temporary=None
    try:
        # UTF-8 BOM makes Japanese source readable by Windows PowerShell 5.1.
        with tempfile.NamedTemporaryFile(prefix='family_monthly_tasks_',suffix='.ps1',delete=False) as stream:
            temporary=Path(stream.name);stream.write(PS_SOURCE.encode('utf-8-sig'));stream.flush();os.fsync(stream.fileno())
        return runner(_powershell_command(temporary,root,action,logon_mode),cwd=str(root),check=False).returncode
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser(description='SBI家族売買記録のWindows月次タスク。初期動作は確認表示だけです。')
    parser.add_argument('--action',choices=('dry-run','install','status','uninstall','test'),default='dry-run')
    parser.add_argument('--logon-mode',choices=('InteractiveToken','Password'),default='InteractiveToken')
    parser.add_argument('--role',choices=('month-end','month-start'),default='month-end')
    args=parser.parse_args(argv)
    try:return run_windows_action(args.action,logon_mode=args.logon_mode,role=args.role)
    except (OSError,RuntimeError,ValueError):
        print('処理を完了できませんでした。Windowsの同じユーザー・Python・タスク実行権限を確認してください。',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
