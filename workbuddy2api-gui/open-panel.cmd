@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem 1) make sure the panel process is up
if not exist "wbgui.exe" (
  echo Missing wbgui.exe in %CD%
  pause
  exit /b 1
)

set "PORT=8787"
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', [int]$env:PORT); $c.Close(); exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo Panel not running - starting it now ...
  call "%~dp0start-gui.cmd"
  rem give the Go http server a moment to bind
  ping -n 4 127.0.0.1 >nul
)

rem 2) wait until the port answers, max ~15s
set "OK=0"
for /L %%i in (1,1,15) do (
  if "!OK!"=="0" (
    powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', [int]$env:PORT); $c.Close(); exit 0 } catch { exit 1 }"
    if not errorlevel 1 set "OK=1"
    if "!OK!"=="0" ping -n 2 127.0.0.1 >nul
  )
)

if "!OK!"=="0" (
  echo Panel did not come up on 127.0.0.1:8787
  echo Check data\gui.err.log for details.
  pause
  exit /b 1
)

echo Opening http://127.0.0.1:8787 ...
start "" http://127.0.0.1:8787
endlocal
