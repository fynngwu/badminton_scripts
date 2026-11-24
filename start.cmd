
@echo off
cd /d "%~dp0"

echo [restart] Stopping previous instance...

powershell.exe ^
  -NoProfile ^
  -ExecutionPolicy Bypass ^
  -File "%~dp0stop.ps1"

if errorlevel 1 (
    echo [restart] Failed to stop previous instance.
    pause
    exit /b 1
)

echo [restart] Starting new instance...

powershell.exe ^
  -NoProfile ^
  -ExecutionPolicy Bypass ^
  -File "%~dp0start.ps1"

pause
