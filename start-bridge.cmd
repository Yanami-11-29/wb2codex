@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM responses_bridge: listens on 7863 (Codex talks to it), forwards to the
REM gateway on 7865 (which only speaks Chat Completions).
REM
REM >>> EDIT THIS: put your gateway api_key here (same value as "api_key"
REM >>> in the gateway's config.json).
set "BR_KEY=REPLACE_WITH_YOUR_GATEWAY_API_KEY"
if "%BR_KEY%"=="REPLACE_WITH_YOUR_GATEWAY_API_KEY" (
  echo.
  echo ERROR: edit start-bridge.cmd and set BR_KEY to your gateway api_key.
  pause
  exit /b 1
)

REM Python resolution order: PATH -> common install paths -> error out.
REM Uses %LOCALAPPDATA% instead of a hard-coded username so this file
REM does not leak the machine account name.
set "PY="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY (
  for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY (
  echo Python 3.8+ not found. Install it, or edit PY in start-bridge.cmd.
  pause
  exit /b 1
)

if not exist "responses_bridge.py" (
  echo Missing responses_bridge.py
  pause
  exit /b 1
)

set "BR_ROOT=%CD%"
set "BR_PID_FILE=%CD%\bridge.pid"
if not exist "data" mkdir "data"

REM Skip if 7863 is already listening
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', 7863); $c.Close(); exit 0 } catch { exit 1 }"
if not errorlevel 1 (
  echo responses_bridge already listening on 127.0.0.1:7863 - skipped.
  exit /b 0
)

powershell -NoProfile -Command "$p = Start-Process -FilePath '%PY%' -ArgumentList 'responses_bridge.py','--listen','127.0.0.1:7863','--upstream','http://127.0.0.1:7865','--key','%BR_KEY%' -WorkingDirectory '%BR_ROOT%' -WindowStyle Hidden -RedirectStandardOutput (Join-Path '%BR_ROOT%' 'data\bridge.out.log') -RedirectStandardError (Join-Path '%BR_ROOT%' 'data\bridge.err.log') -PassThru; [IO.File]::WriteAllText('%BR_PID_FILE%', [string]$p.Id)"
if errorlevel 1 exit /b 1

set /p BR_PID=<"%BR_PID_FILE%"
echo responses_bridge started. PID=%BR_PID%
echo Codex -^> http://127.0.0.1:7863  (Responses)  ==^> gateway http://127.0.0.1:7865 (Chat)
endlocal
