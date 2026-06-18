@echo off
setlocal enabledelayedexpansion

echo.
echo ============================================================================
echo   Kalshi Trading Bot - Windows Setup
echo ============================================================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.11+ from https://www.python.org
    echo (Make sure to check "Add Python to PATH" during installation)
    pause
    exit /b 1
)

echo [1/5] Python found. Creating virtual environment...
if not exist ".venv" (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists, skipping creation.
)

echo [2/5] Activating virtual environment...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment
    pause
    exit /b 1
)

echo [3/5] Installing dependencies (this may take 2-3 minutes)...
pip install -q --upgrade pip
pip install -q -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)

echo [4/5] Setting up configuration...
if not exist ".env" (
    copy .env.example .env >nul
    echo Created .env - you still need to add your API keys (see below)
) else (
    echo .env already exists, skipping.
)

if not exist "secrets" (
    mkdir secrets
    echo Created secrets\ folder
)

echo [5/5] Running offline self-test...
python main.py --self-test
if errorlevel 1 (
    echo WARNING: Self-test failed. Check the error above.
) else (
    echo SELF-TEST PASSED!
)

echo.
echo ============================================================================
echo   Setup Complete!
echo ============================================================================
echo.
echo NEXT STEPS:
echo -----------
echo.
echo 1. Add your API keys to .env:
echo    - KALSHI_API_KEY_ID: your UUID from Kalshi dashboard
echo    - GEMINI_API_KEY: already set in .env
echo    - Private key: secrets\kalshi_private_key.pem (already set up)
echo.
echo 2. Edit .env:
echo    notepad .env
echo.
echo 3. Run the trading bot (Terminal 1):
echo    python main.py
echo.
echo 4. Run the dashboard (Terminal 2):
echo    python dashboard\server.py
echo    Then open: http://localhost:8080
echo.
echo 5. Monitor via the web dashboard:
echo    - Live spot prices + price chart
echo    - Captain's AI reasoning + regime
echo    - Open positions + trade decisions
echo    - Portfolio PnL + circuit breakers
echo    - Live log stream
echo.
echo IMPORTANT:
echo - Always test in PAPER MODE (TRADING_MODE=paper) first
echo - Review trading.log for several minutes before going LIVE
echo - Only set TRADING_MODE=live after validation
echo.
echo For help, see README.md
echo.
pause
