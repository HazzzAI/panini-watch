@echo off
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: deploy_to_vps.bat root@YOUR.SERVER.IP
  echo See DEPLOY.md for how to get a server in 5 minutes.
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\deploy_to_vps.ps1" -Server %1
pause
