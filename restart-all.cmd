@echo off
setlocal
cd /d "%~dp0"

call "%~dp0stop-all.cmd"
if errorlevel 1 (
  echo.
  echo Stop did not fully succeed - aborting before restart.
  pause
  exit /b 1
)

echo.
call "%~dp0start-all.cmd"
endlocal
