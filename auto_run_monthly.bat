@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "MONTHLY_ROLE=%~1"
if "%MONTHLY_ROLE%"=="" set "MONTHLY_ROLE=auto"
if "%MONTHLY_ROLE%"=="auto" goto role_ok
if "%MONTHLY_ROLE%"=="month-end" goto role_ok
if "%MONTHLY_ROLE%"=="month-start" goto role_ok
echo Invalid monthly role.
exit /b 2
:role_ok
where py >nul 2>nul
if errorlevel 1 goto python
py -3 monthly_runner.py --role %MONTHLY_ROLE% --scheduled
set "MONTHLY_RUN_RESULT=%errorlevel%"
goto done
:python
python monthly_runner.py --role %MONTHLY_ROLE% --scheduled
set "MONTHLY_RUN_RESULT=%errorlevel%"
:done
exit /b %MONTHLY_RUN_RESULT%
