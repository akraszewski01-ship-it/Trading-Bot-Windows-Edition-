@echo off
setlocal

REM Always run from the folder this file lives in
cd /d "%~dp0"

echo.
echo ============================================================================
echo   Kalshi Trading Bot
echo ============================================================================
echo.

REM Check setup was run
if not exist ".venv" (
    echo ERROR: Not set up yet. Please run SETUP.bat first.
    echo.
    pause
    exit /b 1
)
if not exist ".env" (
    echo ERROR: No .env file. Please run SETUP.bat first.
    echo.
    pause
    exit /b 1
)

echo Activating environment...
call ".venv\Scripts\activate.bat"

echo Opening dashboard in your browser...
start "" http://localhost:8080

echo.
echo ============================================================================
echo   BOT IS STARTING
echo ============================================================================
echo.
echo   Dashboard:  http://localhost:8080  (opening in your browser)
echo   - Use the SETTINGS tab to enter your Kalshi key
echo   - Use the STOP button (top right) to shut down
echo.
echo   Logs appear below and are saved to trading.log
echo ============================================================================
echo.

REM The dashboard now runs INSIDE the bot (same window, shared data)
python main.py

echo.
echo ============================================================================
echo   Bot has stopped.
echo ============================================================================
echo.
pause
