@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python
py -3 -m pip install -r requirements_gmail.txt
goto done
:python
python -m pip install -r requirements_gmail.txt
:done
pause
