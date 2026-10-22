@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python
py -3 -m unittest discover -v
set "TEST_RESULT=%errorlevel%"
goto done
:python
python -m unittest discover -v
set "TEST_RESULT=%errorlevel%"
:done
if not "%TEST_RESULT%"=="0" echo Tests failed. Read the failures above.
pause
exit /b %TEST_RESULT%
