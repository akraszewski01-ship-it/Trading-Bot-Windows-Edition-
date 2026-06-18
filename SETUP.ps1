# Kalshi Trading Bot - Windows PowerShell Setup
# Usage: PowerShell -ExecutionPolicy Bypass -File SETUP.ps1

param(
    [switch]$SkipTest = $false
)

Write-Host ""
Write-Host "============================================================================"
Write-Host "  Kalshi Trading Bot - Windows Setup"
Write-Host "============================================================================"
Write-Host ""

# Check Python
try {
    $pythonVersion = python --version 2>&1 | Select-Object -First 1
    Write-Host "[✓] Python found: $pythonVersion"
} catch {
    Write-Host "[✗] ERROR: Python not found."
    Write-Host "    Please install Python 3.11+ from https://www.python.org"
    Write-Host "    (Make sure to check 'Add Python to PATH' during installation)"
    Read-Host "Press Enter to exit"
    exit 1
}

# Create venv
Write-Host "[1/5] Setting up virtual environment..."
if (Test-Path ".venv") {
    Write-Host "      Virtual environment already exists, skipping."
} else {
    try {
        python -m venv .venv
        Write-Host "      [✓] Virtual environment created."
    } catch {
        Write-Host "[✗] ERROR: Failed to create virtual environment"
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# Activate venv
Write-Host "[2/5] Activating virtual environment..."
& ".\.venv\Scripts\Activate.ps1"

# Install deps
Write-Host "[3/5] Installing dependencies (this may take 2-3 minutes)..."
try {
    python -m pip install -q --upgrade pip | Out-Null
    pip install -q -r requirements.txt
    Write-Host "      [✓] Dependencies installed."
} catch {
    Write-Host "[✗] ERROR: Failed to install dependencies"
    Read-Host "Press Enter to exit"
    exit 1
}

# Setup config
Write-Host "[4/5] Setting up configuration..."
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "      [✓] Created .env - add your API keys (see below)"
} else {
    Write-Host "      .env already exists, skipping."
}

if (-not (Test-Path "secrets")) {
    New-Item -ItemType Directory -Name "secrets" | Out-Null
    Write-Host "      [✓] Created secrets\ folder"
}

# Self-test
if (-not $SkipTest) {
    Write-Host "[5/5] Running offline self-test..."
    python main.py --self-test
    if ($LASTEXITCODE -ne 0) {
        Write-Host "      WARNING: Self-test failed. Check the error above."
    } else {
        Write-Host "      [✓] SELF-TEST PASSED!"
    }
} else {
    Write-Host "[5/5] Skipping self-test (--SkipTest flag used)"
}

Write-Host ""
Write-Host "============================================================================"
Write-Host "  Setup Complete!"
Write-Host "============================================================================"
Write-Host ""
Write-Host "NEXT STEPS:"
Write-Host "-----------"
Write-Host ""
Write-Host "1. Add your API keys to .env:"
Write-Host "   - KALSHI_API_KEY_ID: your UUID from Kalshi dashboard"
Write-Host "   - GEMINI_API_KEY: already set in .env"
Write-Host "   - Private key: secrets\kalshi_private_key.pem (already set up)"
Write-Host ""
Write-Host "2. Edit .env:"
Write-Host "   notepad .env"
Write-Host ""
Write-Host "3. Run the trading system (paper mode, safe):"
Write-Host "   python main.py"
Write-Host ""
Write-Host "4. Monitor the logs:"
Write-Host "   - Console output (live)"
Write-Host "   - trading.log (rotating file, for audit)"
Write-Host ""
Write-Host "IMPORTANT:"
Write-Host "- Always test in PAPER MODE (TRADING_MODE=paper) first"
Write-Host "- Review trading.log for several minutes before going LIVE"
Write-Host "- Only set TRADING_MODE=live after validation"
Write-Host ""
Write-Host "For help, see README.md"
Write-Host ""
Read-Host "Press Enter to exit"
