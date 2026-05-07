@echo off
setlocal

set "PROJECT_DIR=%~dp0.."
pushd "%PROJECT_DIR%" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Cannot enter project dir: %PROJECT_DIR%
  pause
  exit /b 1
)
set "PROJECT_DIR=%CD%"

set "PORT=8000"
if exist "%PROJECT_DIR%\config.yaml" (
  for /f "tokens=1,2 delims=:" %%A in ('findstr /r /c:"^[ ]*port[ ]*:[ ]*[0-9][0-9]*" "%PROJECT_DIR%\config.yaml"') do (
    set "PORT=%%B"
  )
)
set "PORT=%PORT: =%"
if "%PORT%"=="" set "PORT=8000"

set "PYTHON_CMD="
if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
  set "PYTHON_CMD=%PROJECT_DIR%\.venv\Scripts\python.exe"
) else (
  where py >nul 2>&1
  if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
  ) else (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
  )
)

echo ========================================
echo Kelsey State Machine - Start
echo Project: %PROJECT_DIR%
echo URL: http://127.0.0.1:%PORT%/schedule
echo ========================================

if "%PYTHON_CMD%"=="" (
  echo [ERROR] Python not found.
  echo Please install Python or create .venv first.
  popd >nul 2>&1
  pause
  exit /b 9009
)

if "%PYTHON_CMD%"=="py -3" (
  py -3 -m server.main
) else (
  "%PYTHON_CMD%" -m server.main
)
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo [ERROR] Service exited with code: %EXIT_CODE%
)

popd >nul 2>&1
pause
exit /b %EXIT_CODE%
