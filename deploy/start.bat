@echo off
chcp 65001 >nul
title 凯尔希状态机 - 运行中

set "PROJECT_DIR=%~dp0.."
pushd "%PROJECT_DIR%"
set "PROJECT_DIR=%CD%"

echo ========================================
echo   凯尔希状态机 - 启动服务
echo ========================================
echo.
echo   项目目录: %PROJECT_DIR%
echo   服务地址: http://0.0.0.0:8000
echo   按 Ctrl+C 停止服务
echo.
echo ========================================
echo.

"%PROJECT_DIR%\.venv\Scripts\python.exe" -m server.main

popd
pause
