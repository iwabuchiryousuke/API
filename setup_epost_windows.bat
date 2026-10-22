@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python
py -3 -m pip install -r requirements_epost.txt
if errorlevel 1 goto failed
goto success
:python
python -m pip install -r requirements_epost.txt
if errorlevel 1 goto failed
:success
echo Setup complete. Return to the app and press the PDF collection button.
pause
exit /b 0
:failed
echo Setup failed. Please send a screenshot of this window.
pause
exit /b 1
