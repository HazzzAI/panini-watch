@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\PaniniWatch.lnk'); $s.TargetPath='%~dp0start_hidden.vbs'; $s.WorkingDirectory='%~dp0'; $s.Save()"
echo Panini Watch will now start automatically (hidden) when you log in to Windows.
echo To remove it: delete "PaniniWatch.lnk" from  shell:startup
pause
