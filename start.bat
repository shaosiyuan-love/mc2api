@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ==========================================
echo   mc2api - Windows launcher
echo ==========================================
echo.

REM Unblock files downloaded from the Internet (MOTW)
if exist "%~f0" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { Get-ChildItem -LiteralPath '%~dp0' -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue } catch {}" >nul 2>nul
)

REM Find Python
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY where python3 >nul 2>nul && set "PY=python3"
if not defined PY (
  echo ERROR: Python not found.
  echo Install Python 3.9+ and check "Add python.exe to PATH".
  echo https://www.python.org/downloads/windows/
  echo.
  pause
  exit /b 1
)

echo Using: %PY%
echo.

if not exist "data" mkdir "data"

REM Health check helper -> data\_health_ok.tmp
del /q "data\_health_ok.tmp" >nul 2>nul
%PY% -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:18095/healthz',timeout=2);open(r'data\_health_ok.tmp','w').write('ok')" 2>nul
if exist "data\_health_ok.tmp" (
  echo Already running.
  goto OPEN
)

echo Starting server...
start "mc2api" /MIN %PY% -u "%~dp0server.py"

echo Waiting for health check...
set /a N=0
:WAIT
set /a N+=1
del /q "data\_health_ok.tmp" >nul 2>nul
%PY% -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:18095/healthz',timeout=2);open(r'data\_health_ok.tmp','w').write('ok')" 2>nul
if exist "data\_health_ok.tmp" goto OK
if %N% GEQ 40 goto FAIL
timeout /t 1 /nobreak >nul
goto WAIT

:OK
echo.
echo Started OK.
echo Admin:   http://127.0.0.1:18095/admin
echo Gateway: http://127.0.0.1:18095/v1
if exist "data\default_client_key.txt" (
  echo Default key:
  type "data\default_client_key.txt"
  echo.
)
goto OPEN

:FAIL
echo.
echo Start timeout. Show last log lines:
if exist "data\server.log" (
  powershell -NoProfile -Command "Get-Content -LiteralPath 'data\server.log' -Tail 40 -ErrorAction SilentlyContinue"
) else (
  echo No data\server.log yet. Is Python able to run server.py?
  echo Try: %PY% -u server.py
)
echo.
pause
exit /b 1

:OPEN
echo Opening admin UI...
start "" "http://127.0.0.1:18095/admin"
echo.
echo Closing this window does NOT stop the server.
echo To stop: close the minimized "mc2api" window, or end the python process.
echo.
pause
endlocal
