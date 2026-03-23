@echo off
chcp 65001 >nul
title 凯尔希状态机 - 注册开机自启

set "PROJECT_DIR=%~dp0.."
pushd "%PROJECT_DIR%"
set "PROJECT_DIR=%CD%"
popd

echo ========================================
echo   注册开机自启（Windows 计划任务）
echo ========================================
echo.

:: 删除旧任务（如果存在）
schtasks /delete /tn "KelseyStateMachine" /f >nul 2>&1

:: 创建启动脚本（不弹窗版本，使用 pythonw 静默运行）
set "SILENT_SCRIPT=%PROJECT_DIR%\deploy\_run_silent.vbs"
echo Set WshShell = CreateObject("WScript.Shell") > "%SILENT_SCRIPT%"
echo WshShell.CurrentDirectory = "%PROJECT_DIR%" >> "%SILENT_SCRIPT%"
echo WshShell.Run """%PROJECT_DIR%\.venv\Scripts\pythonw.exe"" -m server.main", 0, False >> "%SILENT_SCRIPT%"

:: 注册计划任务：开机时以最高权限运行
schtasks /create /tn "KelseyStateMachine" /tr "wscript.exe \"%SILENT_SCRIPT%\"" /sc onstart /ru SYSTEM /rl highest /f
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [OK] 开机自启已注册！
    echo     任务名: KelseyStateMachine
    echo     触发器: 系统启动时
    echo     运行方式: 后台静默运行
    echo.
    echo   管理方式:
    echo     查看: schtasks /query /tn "KelseyStateMachine"
    echo     删除: schtasks /delete /tn "KelseyStateMachine" /f
    echo     手动触发: schtasks /run /tn "KelseyStateMachine"
) else (
    echo.
    echo [!] 注册失败，请以管理员身份运行此脚本
)

echo.
pause
