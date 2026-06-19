@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "LOG=setup-log.txt"
echo Kalshi Trading Bot - setup log > "%LOG%"
echo Started: %date% %time% >> "%LOG%"
echo Folder : %~dp0 >> "%LOG%"

echo.
echo ============================================================================
echo   Kalshi Trading Bot - Windows Setup
echo ============================================================================
echo.
echo This will:
echo   1. Check for Python
echo   2. Create a virtual environment
echo   3. Install dependencies (takes 2-4 minutes)
echo   4. Run a self-test
echo.

REM --- PYTHON DETECTION (simple, no piping) ---
echo [1/6] Detecting Python...

py --version > nul 2>&1
if errorlevel 1 goto try_python
set "PY=py"
py --version
py --version >> "%LOG%"
echo       OK - using 'py' launcher
goto python_found

:try_python
python --version > nul 2>&1
if errorlevel 1 goto no_python
set "PY=python"
python --version
python --version >> "%LOG%"
echo       OK - using 'python' command
goto python_found

:no_python
echo.
echo ============================================================================
echo ERROR: Python NOT FOUND
echo ============================================================================
echo.
echo You need Python 3.11 or newer. Download from:
echo   https://www.python.org/downloads/
echo.
echo When installing:
echo   - CHECK the box "Add Python to PATH" at the bottom left
echo   - Click "Install Now"
echo   - Restart your computer
echo   - Try this setup script again
echo.
pause
exit /b 1

:python_found

REM --- CREATE VENV ---
echo.
echo [2/6] Creating virtual environment (.venv)...
if exist ".venv" (
    echo       (already exists)
) else (
    echo       Running: %PY% -m venv .venv
    %PY% -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create venv
        pause
        exit /b 1
    )
)

REM --- ACTIVATE VENV ---
echo.
echo [3/6] Activating virtual environment...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo ERROR: Failed to activate venv
    pause
    exit /b 1
)
echo       OK

REM --- INSTALL DEPS ---
echo.
echo [4/6] Installing Python dependencies...
echo       (this downloads packages and can take 2-4 minutes)
echo       Upgrading pip...
python -m pip install --upgrade pip --quiet
echo       Installing CORE requirements (bot + dashboard + Gemini)...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install core requirements.
    echo If you saw "filename or extension is too long", move this folder to a
    echo short path like  C:\Bot  and run SETUP.bat again.
    pause
    exit /b 1
)
echo       Core OK

echo.
echo       Installing OPTIONAL TimesFM model (PyTorch, large)...
echo       This is best-effort - if it fails the bot still runs with its
echo       built-in baseline forecaster.
python -m pip install -r requirements-optional.txt
if errorlevel 1 (
    echo       NOTE: TimesFM/PyTorch did not install. That is OK - the bot
    echo       will use the baseline forecaster. You can retry later with:
    echo         .venv\Scripts\activate ^&^& pip install -r requirements-optional.txt
) else (
    echo       TimesFM OK
)

REM --- SETUP CONFIG ---
echo.
echo [5/6] Setting up configuration...
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" > nul
        echo       Created .env file
    )
)
if not exist "secrets" mkdir secrets
echo       OK

REM --- SELF-TEST ---
echo.
echo [6/6] Running self-test...
python main.py --self-test > selftest-output.txt 2>&1

REM Show the output
echo.
type selftest-output.txt

REM Check if it passed
findstr /C:"SELF-TEST OK" selftest-output.txt > nul
if errorlevel 1 (
    echo.
    echo ============================================================================
    echo ERROR: Self-test failed
    echo ============================================================================
    echo.
    echo Check the output above. Full details saved in:
    echo   setup-log.txt
    echo.
    type selftest-output.txt >> "%LOG%"
    del selftest-output.txt
    pause
    exit /b 1
)

del selftest-output.txt
type selftest-output.txt >> "%LOG%"

echo.
echo ============================================================================
echo   SUCCESS - Setup Complete
echo ============================================================================
echo.
echo.
echo Press any key to START THE BOT now
echo (or close this window to start it later with START.bat)
echo.
pause

echo.
echo Starting the bot...
call START.bat
exit /b 0
