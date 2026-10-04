@echo off
cd /d "%~dp0"
title Panini Watch
:loop
python -m panini_watch run
echo.
echo Panini Watch stopped. Restarting in 30 seconds (close this window to quit)...
timeout /t 30 >nul
goto loop
