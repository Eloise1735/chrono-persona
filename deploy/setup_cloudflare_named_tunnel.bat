@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%setup_cloudflare_named_tunnel.ps1"

if not exist "%PS_SCRIPT%" (
  echo [ERROR] Missing PowerShell script: %PS_SCRIPT%
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Named tunnel setup failed with code %EXIT_CODE%.
  pause
  exit /b %EXIT_CODE%
)

echo.
echo [OK] Named tunnel setup finished.
pause
exit /b 0
