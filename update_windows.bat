@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 家族売買記録アプリを終了してから、コードZIPを選択してください。
where py >nul 2>nul
if errorlevel 1 goto python
py -3 code_update.py
set "UPDATE_RESULT=%errorlevel%"
goto done
:python
python code_update.py
set "UPDATE_RESULT=%errorlevel%"
:done
if not "%UPDATE_RESULT%"=="0" (
  echo コード更新を完了できませんでした。上のメッセージを確認してください。
  pause
)
exit /b %UPDATE_RESULT%
