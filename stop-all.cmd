@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Stopping web panel (wbgui :8787) ...
powershell -NoProfile -Command "Get-Process -Name wbgui -ErrorAction SilentlyContinue | Stop-Process -Force"

echo [2/3] Stopping responses bridge (:7863) ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*responses_bridge.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

echo [3/3] Stopping gateway (wb2api :7865) ...
powershell -NoProfile -Command "Get-Process -Name wb2api -ErrorAction SilentlyContinue | Stop-Process -Force"

del /q "%~dp0bridge.pid" >nul 2>&1
del /q "%~dp0wb2api.pid" >nul 2>&1
del /q "%~dp0workbuddy2api-gui\wbgui.pid" >nul 2>&1

echo Waiting for ports to release ...
powershell -NoProfile -Command "Start-Sleep -Seconds 3"

set "BUSY=0"
for %%P in (7865 7863 8787) do (
  powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', %%P); $c.Close(); exit 0 } catch { exit 1 }"
  if not errorlevel 1 (
    echo   STILL OCCUPIED: %%P
    set "BUSY=1"
  )
)

if "%BUSY%"=="1" (
  echo.
  echo Some ports are still held. Find the owners with:
  echo     netstat -ano ^| findstr ":7865 :7863 :8787"
  exit /b 1
)

echo All stopped: 7865 gateway, 7863 bridge, 8787 panel.
endlocal
