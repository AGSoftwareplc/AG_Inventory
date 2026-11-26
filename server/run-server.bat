@echo off
REM Runs Mizan on a server with no one logged in (started by the "AG Inventory" scheduled task).
REM If the app ever stops, it starts again after 10 seconds. Output goes to logs\server.log.
REM Optional first argument: the port (default 5000).
cd /d "%~dp0.."
if not "%~1"=="" set PORT=%~1
set AGINV_NO_BROWSER=1
set PYTHONUNBUFFERED=1
if not exist logs mkdir logs
:loop
echo [%date% %time%] Starting Mizan >> logs\server.log
.venv\Scripts\python.exe serve.py >> logs\server.log 2>&1
echo [%date% %time%] Mizan stopped, starting again in 10 seconds >> logs\server.log
timeout /t 10 /nobreak > nul
goto loop
