# Python PATH Not Found — Quick Fix

If you see: **"ERROR: Python not found. Please install Python 3.11..."**

## Try these solutions (in order):

### 1. Use the Python launcher (easiest)
Windows comes with `py` command. Try this in Command Prompt:
```
py --version
```

If it works, the setup script will now use it automatically.

---

### 2. Reinstall Python with PATH enabled
1. Go to **https://www.python.org/downloads/**
2. Download **Python 3.11** or **3.12**
3. Run the installer
4. **IMPORTANT**: Check the box: **"Add Python to PATH"** (bottom left)
5. Click **"Install Now"**
6. Restart your computer
7. Try the SETUP.bat again

---

### 3. Manual PATH fix (if already installed)
If Python is installed but not in PATH:

1. **Find Python folder**
   - Usually: `C:\Users\[YourName]\AppData\Local\Programs\Python\Python311`
   - Or: `C:\Program Files\Python311`

2. **Add to PATH**
   - Press `Win + X` → Choose **System**
   - Click **Advanced system settings** (left panel)
   - Click **Environment Variables** (bottom right)
   - Under "User variables", select **PATH** → **Edit**
   - Click **New** and paste your Python folder path
   - Click **OK** three times
   - Restart Command Prompt and try `python --version`

3. **Verify**
   ```
   python --version
   ```
   Should show: `Python 3.11.x` or higher

---

### 4. Test it works
Open Command Prompt and type:
```
python --version
```

If you see `Python 3.11` (or higher), you're good! Run SETUP.bat again.

---

### Still stuck?
Try opening Command Prompt as Administrator and running:
```
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
python main.py --self-test
```

This bypasses the batch script and uses explicit Python launcher syntax.
