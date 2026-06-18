@echo off
REM Start ONLY the dashboard (open this in a second window).
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python dashboard\server.py
pause
