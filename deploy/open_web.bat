@echo off
chcp 65001 >nul
title 凯尔希状态机 - 打开网页入口

set "PROJECT_DIR=%~dp0.."
pushd "%PROJECT_DIR%"
set "PROJECT_DIR=%CD%"
popd

set "WEB_URL="
set "URL_FILE=%PROJECT_DIR%\deploy\web_url.txt"

:: 支持命令行覆盖：
:: open_web.bat http://47.115.35.155:8000
:: open_web.bat 47.115.35.155 8000
if not "%~1"=="" (
    set "WEB_URL=%~1"
)
if not "%~2"=="" (
    set "WEB_URL=http://%~1:%~2"
)

if "%WEB_URL%"=="" (
    if exist "%URL_FILE%" (
        for /f "usebackq tokens=* delims=" %%i in ("%URL_FILE%") do (
            set "WEB_URL=%%i"
            goto :open
        )
    )
)

if "%WEB_URL%"=="" (
    set "WEB_URL=http://127.0.0.1:8000"
)

:open
echo [OK] 打开网页入口: %WEB_URL%
start "" "%WEB_URL%"
exit /b 0
