@echo off
setlocal

echo.
echo ============================================================================
echo   Starting Kalshi Trading Bot
echo ============================================================================
echo.

REM Make sure setup has been run
if not exist ".venv" (
    echo ERROR: Not set up yet. Please run SETUP.bat first.
    pause
    exit /b 1
)

REM Make sure .env exists
if not exist ".env" (
    echo ERROR: No .env file found. Please run SETUP.bat first.
    pause
    exit /b 1
)

echo Activating environment...
call .venv\Scripts\activate.bat

echo.
echo Launching DASHBOARD in a new window...
start "Kalshi Dashboard" cmd /k ".venv\Scripts\activate.bat && python dashboard\server.py"

echo Waiting for dashboard to start...
timeout /t 4 /nobreak >nul

echo Opening dashboard in your browser...
start http://localhost:8080

echo.
echo ============================================================================
echo   TRADING BOT IS STARTING (this window)
echo ============================================================================
echo.
echo   Dashboard:  http://localhost:8080  (opened in browser)
echo   Bot logs:   shown below + saved to trading.log
echo.
echo   To STOP: close both windows or press Ctrl+C
echo ============================================================================
echo.

REM Start the trading bot in THIS window
python main.py

echo.
echo Bot stopped. Press any key to exit.
pause >nul
