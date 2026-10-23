@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 今の資産取得をテストします。月末・月初16時の取得成功としては記録しません。PDF取得は行いません。
where py >nul 2>nul
if errorlevel 1 goto python
py -3 windows_tasks.py --action test
set "MONTHLY_TASK_RESULT=%errorlevel%"
goto done
:python
python windows_tasks.py --action test
set "MONTHLY_TASK_RESULT=%errorlevel%"
:done
if not "%MONTHLY_TASK_RESULT%"=="0" echo テストを完了できませんでした。アプリの自動取得履歴を確認してください。
pause
exit /b %MONTHLY_TASK_RESULT%
