@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ==========================================
echo   mc2api — Windows 一键启动
echo ==========================================
echo.

REM Prefer py launcher, then python, then python3
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY where python3 >nul 2>nul && set "PY=python3"
if not defined PY (
  echo 错误: 未找到 Python。请安装 Python 3.9+ 并勾选 Add python.exe to PATH
  echo 下载: https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

echo 使用: %PY%
echo.

if not exist "data" mkdir data

REM Already up?
%PY% -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18095/healthz', timeout=2)" >nul 2>nul
if %ERRORLEVEL%==0 (
  echo 已在运行。
  goto :open
)

echo 正在启动...
start "mc2api" /MIN %PY% -u "%~dp0server.py"

REM Wait for healthz
set /a n=0
:wait
set /a n+=1
%PY% -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18095/healthz', timeout=2)" >nul 2>nul
if %ERRORLEVEL%==0 goto :ok
if %n% geq 40 goto :fail
timeout /t 1 /nobreak >nul
goto :wait

:ok
echo 启动成功
echo 管理台: http://127.0.0.1:18095/admin
echo 网关:   http://127.0.0.1:18095/v1
if exist "data\default_client_key.txt" (
  echo 默认 Key:
  type "data\default_client_key.txt"
  echo.
)
goto :open

:fail
echo 启动超时，请查看 data\server.log
if exist "data\server.log" (
  echo ---- 最近日志 ----
  powershell -NoProfile -Command "Get-Content -Path 'data\server.log' -Tail 40 -ErrorAction SilentlyContinue"
)
pause
exit /b 1

:open
echo 正在打开管理台...
start "" "http://127.0.0.1:18095/admin"
echo.
echo 关闭本窗口不会停止服务。
echo 停止服务: 关闭标题为 mc2api 的最小化窗口，或结束 python 进程。
echo.
pause
endlocal
