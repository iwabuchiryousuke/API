@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "MONTHLY_LOGON=InteractiveToken"
if "%~1"=="" goto mode_ok
if /I "%~1"=="Password" (
  set "MONTHLY_LOGON=Password"
  goto mode_ok
)
if /I "%~1"=="InteractiveToken" goto mode_ok
echo 使用方法: setup_monthly_tasks.bat [InteractiveToken または Password]
exit /b 2
:mode_ok
where py >nul 2>nul
if errorlevel 1 goto python
py -3 windows_tasks.py --action install --logon-mode %MONTHLY_LOGON%
set "MONTHLY_TASK_RESULT=%errorlevel%"
goto done
:python
python windows_tasks.py --action install --logon-mode %MONTHLY_LOGON%
set "MONTHLY_TASK_RESULT=%errorlevel%"
:done
if not "%MONTHLY_TASK_RESULT%"=="0" echo 登録できませんでした。Windowsの権限・タイムゾーン・実行ポリシーを確認してください。
pause
exit /b %MONTHLY_TASK_RESULT%
