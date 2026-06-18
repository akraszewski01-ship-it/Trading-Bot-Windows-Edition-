@echo off
REM Simplest possible way to start the bot.
REM Double-click this file after SETUP.bat has already been run.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python main.py
pause
