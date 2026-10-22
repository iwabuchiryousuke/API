@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 app.py
) else (
  python app.py
)
if errorlevel 1 (
  echo.
  echo 起動できませんでした。Python 3.10以降とTkinterを確認してください。
  echo はじめに.txt の手順をご覧ください。
  pause
)
