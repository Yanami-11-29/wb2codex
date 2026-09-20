@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Starting gateway (wb2api :7865) ...
if not exist "wb2api.exe" (
  echo.
  echo Missing wb2api.exe - build it first, see README.md step 1.
  pause
  exit /b 1
)
call "%~dp0start-workbuddy2api.cmd"
if errorlevel 1 (
  echo Gateway failed to start. Check config.json and wb2api.exe.
  pause
  exit /b 1
)

echo [2/3] Starting responses-bridge (:7863 -^> :7865) ...
call "%~dp0start-bridge.cmd"
if errorlevel 1 (
  echo Bridge failed to start. Gateway is still running; Codex will NOT work.
  pause
  exit /b 1
)

echo [3/3] Starting web panel (wbgui :8787) ...
call "%~dp0workbuddy2api-gui\start-gui.cmd"
if errorlevel 1 (
  echo Panel failed to start. Gateway + bridge are still running.
  pause
  exit /b 1
)

echo.
echo Gateway  : http://127.0.0.1:7865/healthz   (Chat Completions)
echo Bridge   : http://127.0.0.1:7863           (Responses -^> Chat, for Codex)
echo Web panel: http://127.0.0.1:8787
echo.
echo Codex/cc-switch: leave base_url as http://127.0.0.1:7863/v1 - no change needed.
echo.
echo Panel login: the password you set in workbuddy2api-gui\config.json
pause
endlocal
