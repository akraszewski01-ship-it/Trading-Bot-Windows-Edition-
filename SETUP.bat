@echo off
setlocal enabledelayedexpansion

REM --- Always run from the folder this file lives in --------------------------
cd /d "%~dp0"

REM --- Everything also gets written to setup-log.txt so nothing is lost --------
set "LOG=setup-log.txt"
echo Kalshi Trading Bot - setup log > "%LOG%"
echo Started: %date% %time% >> "%LOG%"
echo Folder : %~dp0 >> "%LOG%"

echo.
echo ============================================================================
echo   Kalshi Trading Bot - Windows Setup
echo ============================================================================
echo.
echo   A full copy of this output is being saved to:  setup-log.txt
echo   If anything fails, open that file (or send it over) to diagnose.
echo.

REM --- 1. Find Python (try the 'py' launcher first, then 'python') ------------
set "PY="
py --version >nul 2>&1
if not errorlevel 1 (
    set "PY=py"
) else (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if "%PY%"=="" (
    echo PYTHON NOT FOUND >> "%LOG%"
    echo ERROR: Python was not found on this computer.
    echo.
    echo   Fix it like this:
    echo     1. Go to  https://www.python.org/downloads/
    echo     2. Download Python 3.11 or newer and run the installer
    echo     3. TICK the box "Add Python to PATH" at the bottom
    echo     4. Click Install, then RESTART your computer
    echo     5. Double-click SETUP.bat again
    echo.
    echo   (More help is in PYTHON-PATH-FIX.md)
    echo.
    echo ----------------------------------------------------------------------------
    echo   Press any key to close this window.
    pause >nul
    exit /b 1
)

echo [OK] Python found using: %PY%
%PY% --version
%PY% --version >> "%LOG%" 2>&1
echo Using: %PY% >> "%LOG%"

REM --- 2. Create the virtual environment -------------------------------------
echo.
echo [1/5] Creating virtual environment (.venv)...
if not exist ".venv" (
    %PY% -m venv .venv >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo VENV CREATION FAILED >> "%LOG%"
        echo ERROR: Could not create the virtual environment.
        echo        See setup-log.txt for details.
        echo.
        echo   Press any key to close this window.
        pause >nul
        exit /b 1
    )
) else (
    echo       .venv already exists - reusing it.
)

REM --- 3. Activate it --------------------------------------------------------
echo [2/5] Activating virtual environment...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo VENV ACTIVATION FAILED >> "%LOG%"
    echo ERROR: Could not activate the virtual environment.
    echo        Try deleting the .venv folder and running SETUP.bat again.
    echo.
    echo   Press any key to close this window.
    pause >nul
    exit /b 1
)

REM --- 4. Install dependencies ------------------------------------------------
echo [3/5] Installing dependencies...
echo       This downloads several packages and can take 2-4 minutes.
echo       The window may look frozen during install - please WAIT, do not close it.
echo.
python -m pip install --upgrade pip >> "%LOG%" 2>&1
python -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
    echo PIP INSTALL FAILED >> "%LOG%"
    echo ERROR: Some dependencies failed to install.
    echo        The details are in setup-log.txt.
    echo.
    echo        You can try again, or install manually:
    echo            .venv\Scripts\activate
    echo            pip install -r requirements.txt
    echo.
    echo   Press any key to close this window.
    pause >nul
    exit /b 1
)
echo       Dependencies installed.

REM --- 5. Configuration files -------------------------------------------------
echo [4/5] Setting up configuration...
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo       Created .env  (remember to add your API keys).
    ) else (
        echo       NOTE: no .env or .env.example found.
    )
) else (
    echo       .env already exists - keeping it.
)
if not exist "secrets" mkdir secrets

REM --- 6. Offline self-test --------------------------------------------------
echo [5/5] Running offline self-test...
echo. >> "%LOG%"
echo ===== SELF-TEST OUTPUT ===== >> "%LOG%"
python main.py --self-test > selftest-output.txt 2>&1
type selftest-output.txt
type selftest-output.txt >> "%LOG%"

set "SELFTEST_OK=0"
findstr /C:"SELF-TEST OK" selftest-output.txt >nul 2>&1
if not errorlevel 1 set "SELFTEST_OK=1"
del selftest-output.txt >nul 2>&1

echo.
echo ============================================================================
if "%SELFTEST_OK%"=="1" (
    echo   SETUP COMPLETE - SELF-TEST PASSED
    echo ============================================================================
    echo.
    echo   The bot is ready to run in PAPER mode (no real money).
    echo.
    echo   Press any key to START THE BOT now,
    echo   or just close this window to start it later with START.bat
    echo.
    pause >nul
    echo Launching the bot...
    call "%~dp0START.bat"
    exit /b 0
) else (
    echo   SETUP FINISHED, BUT THE SELF-TEST DID NOT PASS
    echo ============================================================================
    echo.
    echo   Please open  setup-log.txt  in this folder and review the errors,
    echo   or send that file over so it can be diagnosed.
    echo.
    echo   Press any key to close this window.
    pause >nul
    exit /b 1
)
