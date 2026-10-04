@echo off
cd /d "%~dp0"
echo === Panini Watch setup ===
python --version >nul 2>&1 || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
python -m pip install -r requirements.txt
rem Uses your installed Google Chrome; only downloads a browser if Chrome is missing
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" >nul 2>&1 || python -m playwright install chromium
python -m panini_watch setup
pause
