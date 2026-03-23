@echo off
chcp 65001 >nul
title 凯尔希状态机 - 开发模式

set "PROJECT_DIR=%~dp0.."
pushd "%PROJECT_DIR%"
set "PROJECT_DIR=%CD%"

set KELSEY_DEV=1

echo ========================================
echo   凯尔希状态机 - 开发模式（热重载）
echo ========================================
echo.

"%PROJECT_DIR%\.venv\Scripts\python.exe" -m server.main

popd
pause
