# 📥 Download & Install Kalshi Trading Bot

## One-Click Download

1. **Go to [Releases](../../releases)** on this GitHub repository
2. **Click the latest release** (top of the list)
3. **Download** `kalshi-trading-bot.zip` under "Assets"

---

## Install (Windows)

1. **Extract** the ZIP file to a folder
   - Right-click → Extract All

2. **Double-click `SETUP.bat`**
   - This runs once, automatically:
     - ✅ Checks for Python (installs it if needed)
     - ✅ Creates a virtual environment
     - ✅ Installs all dependencies (~2-3 min)
     - ✅ Runs an offline self-test
     - ✅ Shows next steps

3. **Edit `.env`**
   ```
   notepad .env
   ```
   Add your API keys:
   - `KALSHI_API_KEY_ID=` (your UUID from Kalshi dashboard)
   - `GEMINI_API_KEY=` (your key from Google AI Studio)

4. **Run the bot**
   ```
   python main.py
   ```
   Starts in **paper mode** (safe testing) by default.

---

## If SETUP.bat doesn't work

Use PowerShell instead:
```powershell
PowerShell -ExecutionPolicy Bypass -File SETUP.ps1
```

---

## Before Going Live

1. Run in **paper mode** for 15–30 minutes
2. Review `trading.log` to verify behavior
3. Check the README.md for configuration details
4. Only set `TRADING_MODE=live` after testing

---

## Troubleshooting

**"Python not found"**
- Install Python 3.11+ from https://www.python.org
- Make sure to check "Add Python to PATH"
- Restart after installing

**"Permission denied" (PowerShell)**
- Run as Administrator, or:
  ```powershell
  Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope CurrentUser
  ```

**Other issues?**
- Check `trading.log` for details
- See `README.md` for architecture & config reference

---

Happy trading! 🚀
