@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python
py -3 -m pip install -r requirements_pdf.txt -r requirements_epost.txt -r requirements_gmail.txt
goto done
:python
python -m pip install -r requirements_pdf.txt -r requirements_epost.txt -r requirements_gmail.txt
:done
if errorlevel 1 (echo Setup failed. Check the message above.) else (echo Setup complete. Run start_windows.bat.)
pause
