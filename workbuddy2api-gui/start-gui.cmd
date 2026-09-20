@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if not exist "wbgui.exe" (
  echo Missing wbgui.exe. Build it with: go build -o wbgui.exe ./cmd/server
  exit /b 1
)
if not exist "config.json" (
  echo Missing config.json. Copy config.example.json and configure it first.
  exit /b 1
)

set "GUI_ROOT=%CD%"
set "GUI_EXE=%CD%\wbgui.exe"
set "GUI_PID_FILE=%CD%\wbgui.pid"
if exist "%GUI_PID_FILE%" (
  set /p GUI_PID=<"%GUI_PID_FILE%"
  powershell -NoProfile -Command "try { $p=Get-Process -Id ([int]$env:GUI_PID) -ErrorAction Stop; if ([IO.Path]::GetFullPath($p.Path) -eq [IO.Path]::GetFullPath($env:GUI_EXE)) { exit 0 } } catch {}; exit 1"
  if not errorlevel 1 (
    echo WorkBuddy2API-GUI is already running. PID=!GUI_PID!
    exit /b 0
  )
  del /q "%GUI_PID_FILE%" >nul 2>&1
)

if not exist "data" mkdir "data"
powershell -NoProfile -Command "$p=Start-Process -FilePath $env:GUI_EXE -ArgumentList '-config','config.json' -WorkingDirectory $env:GUI_ROOT -WindowStyle Hidden -RedirectStandardOutput (Join-Path $env:GUI_ROOT 'data\gui.out.log') -RedirectStandardError (Join-Path $env:GUI_ROOT 'data\gui.err.log') -PassThru; [IO.File]::WriteAllText($env:GUI_PID_FILE, [string]$p.Id)"
if errorlevel 1 exit /b 1

set /p GUI_PID=<"%GUI_PID_FILE%"
echo WorkBuddy2API-GUI started. PID=%GUI_PID%
echo Panel URL: http://127.0.0.1:8787
endlocal
