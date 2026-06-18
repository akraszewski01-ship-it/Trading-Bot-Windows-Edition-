@echo off
setlocal

REM ── Always run from the folder this .bat file lives in ──────────────────────
cd /d "%~dp0"

echo.
echo ============================================================================
echo   Kalshi Trading Bot  ^|  Starting up...
echo ============================================================================
echo.
echo   Folder : %~dp0
echo   Time   : %date% %time%
echo.

REM ── Check SETUP was run ─────────────────────────────────────────────────────
if not exist ".venv" (
    echo ERROR: .venv folder not found. SETUP.bat has not been run yet.
    echo.
    echo   Please double-click SETUP.bat first, then try START.bat again.
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo ERROR: .env file not found.
    echo.
    echo   Please double-click SETUP.bat first, then try START.bat again.
    echo.
    pause
    exit /b 1
)

REM ── Activate the virtual environment ────────────────────────────────────────
echo [1/4] Activating Python environment...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo ERROR: Could not activate virtual environment.
    echo        Try deleting the .venv folder and running SETUP.bat again.
    echo.
    pause
    exit /b 1
)

REM ── Quick sanity check ──────────────────────────────────────────────────────
echo [2/4] Checking Python...
python --version
if errorlevel 1 (
    echo ERROR: python command failed inside the virtual environment.
    echo        Delete .venv and run SETUP.bat again.
    echo.
    pause
    exit /b 1
)

REM ── Launch dashboard in a separate window ───────────────────────────────────
echo [3/4] Launching dashboard window...
start "Kalshi Dashboard" cmd /k "cd /d "%~dp0" && ".venv\Scripts\activate.bat" && python dashboard\server.py"

echo        Waiting 5 seconds for dashboard to start...
timeout /t 5 /nobreak >nul

echo        Opening http://localhost:8080 in your browser...
start http://localhost:8080

REM ── Start the bot in THIS window ─────────────────────────────────────────────
echo [4/4] Starting trading bot (logs appear below)...
echo.
echo ============================================================================
echo   LIVE  --  Dashboard: http://localhost:8080   --  Ctrl+C to stop
echo ============================================================================
echo.

python main.py

echo.
echo ============================================================================
echo   Bot has stopped.
echo ============================================================================
echo.
pause
