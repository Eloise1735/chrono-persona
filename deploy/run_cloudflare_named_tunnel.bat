@echo off
setlocal

set "CONFIG=%USERPROFILE%\.cloudflared\kelsey-config.yml"

if not "%~1"=="" (
  set "CONFIG=%~1"
)

where cloudflared >nul 2>&1
if errorlevel 1 (
  echo [ERROR] cloudflared was not found on PATH.
  pause
  exit /b 9009
)

if not exist "%CONFIG%" (
  echo [ERROR] Tunnel config not found: %CONFIG%
  echo Run deploy\setup_cloudflare_named_tunnel.bat first.
  pause
  exit /b 1
)

echo ========================================
echo Cloudflare Named Tunnel
echo Config: %CONFIG%
echo ========================================

cloudflared tunnel --config "%CONFIG%" run
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] cloudflared exited with code %EXIT_CODE%.
)

pause
exit /b %EXIT_CODE%
