@echo off
setlocal
cd /d "%~dp0"
echo Unblocking files under: %CD%
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -LiteralPath '%CD%' -Recurse -File | Unblock-File; Write-Host 'Done. You can now double-click start.bat'"
echo.
pause
