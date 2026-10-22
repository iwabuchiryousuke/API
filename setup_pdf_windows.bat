@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 -m pip install -r requirements_pdf.txt
) else (
  python -m pip install -r requirements_pdf.txt
)
if errorlevel 1 (
  echo PDF機能の準備に失敗しました。表示されたメッセージを共有してください。
) else (
  echo PDF機能の準備ができました。start_windows.bat から起動してください。
)
pause
