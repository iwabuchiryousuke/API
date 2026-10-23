@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python
py -3 windows_tasks.py --action dry-run
set "MONTHLY_TASK_RESULT=%errorlevel%"
goto done
:python
python windows_tasks.py --action dry-run
set "MONTHLY_TASK_RESULT=%errorlevel%"
:done
if not "%MONTHLY_TASK_RESULT%"=="0" echo 完了できませんでした。上のメッセージを確認してください。
pause
exit /b %MONTHLY_TASK_RESULT%
