@echo off
REM One-time installation on Windows. Needs Python 3.10+ from python.org ("Add python.exe to PATH" ticked).
cd /d "%~dp0"
python -m venv .venv || (echo Python was not found. Install it from https://www.python.org/downloads/ & pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
echo.
echo Installation finished. Double-click start.bat to run AG Inventory.
pause
