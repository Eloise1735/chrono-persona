@echo off
setlocal EnableExtensions

cd /d "%~dp0\.."

set PYTHON_CMD=python
if exist ".venv\Scripts\python.exe" set PYTHON_CMD=.venv\Scripts\python.exe

echo [1/2] Reading OB embedding runtime settings...
%PYTHON_CMD% migrate\check_ob_embedding_config.py
if errorlevel 1 (
  echo ERROR: Embedding settings are incomplete or unavailable.
  exit /b 1
)

echo.
echo [2/2] Probing embeddings endpoint...
%PYTHON_CMD% migrate\check_ob_embedding_config.py --probe
if errorlevel 1 (
  echo ERROR: Embedding endpoint probe failed.
  exit /b 1
)

echo.
echo OB embedding config and endpoint look healthy.
exit /b 0
