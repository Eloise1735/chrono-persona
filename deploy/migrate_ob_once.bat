@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0\.."

if not exist "data" mkdir "data"
if not exist "data\migration_logs" mkdir "data\migration_logs"

set TS=%DATE:/=-%_%TIME::=-%
set TS=%TS: =0%
set TS=%TS:.=-%
set TS=%TS:\=-%
set LOG_FILE=data\migration_logs\ob_migration_%TS%.log

set PYTHON_CMD=python
if exist ".venv\Scripts\python.exe" set PYTHON_CMD=.venv\Scripts\python.exe

call :log "============================================================"
call :log "OB migration started at %date% %time%"
call :log "Workspace: %cd%"
call :log "Python: %PYTHON_CMD%"
call :log "Log: %LOG_FILE%"
call :log "============================================================"

call :blank
call :log "[1/4] Checking Python..."
%PYTHON_CMD% --version >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  call :log "ERROR: Python is not available."
  goto :fail
)

call :blank
call :log "[2/4] Dry-run legacy_to_ob.py..."
%PYTHON_CMD% migrate\legacy_to_ob.py --dry-run >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  call :log "ERROR: legacy_to_ob.py dry-run failed. See %LOG_FILE%"
  goto :fail
)

call :blank
call :log "[3/4] Running legacy_to_ob.py..."
%PYTHON_CMD% migrate\legacy_to_ob.py >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  call :log "ERROR: legacy_to_ob.py failed. See %LOG_FILE%"
  goto :fail
)

call :blank
call :log "[4/4] Running backfill_ob_embeddings.py --resume..."
%PYTHON_CMD% migrate\backfill_ob_embeddings.py --batch-size 20 --resume >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  call :log "ERROR: backfill_ob_embeddings.py failed. See %LOG_FILE%"
  goto :fail
)

call :blank
call :log "============================================================"
call :log "OB migration finished successfully at %date% %time%"
call :log "============================================================"
echo Done. Log written to %LOG_FILE%
exit /b 0

:fail
call :blank
call :log "============================================================"
call :log "OB migration FAILED at %date% %time%"
call :log "Check log: %LOG_FILE%"
call :log "============================================================"
exit /b 1

:log
echo %~1
>> "%LOG_FILE%" echo %~1
exit /b 0

:blank
echo.
>> "%LOG_FILE%" echo.
exit /b 0
