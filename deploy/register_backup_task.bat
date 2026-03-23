@echo off
chcp 65001 >nul
title 凯尔希状态机 - 注册每日自动备份

set "PROJECT_DIR=%~dp0.."
pushd "%PROJECT_DIR%"
set "PROJECT_DIR=%CD%"
popd

echo ========================================
echo   注册每日自动备份（凌晨 4:00）
echo ========================================
echo.

schtasks /delete /tn "KelseyBackup" /f >nul 2>&1

schtasks /create /tn "KelseyBackup" /tr "\"%PROJECT_DIR%\deploy\backup.bat\"" /sc daily /st 04:00 /ru SYSTEM /rl highest /f
if %ERRORLEVEL% EQU 0 (
    echo [OK] 每日备份任务已注册！
    echo     任务名: KelseyBackup
    echo     执行时间: 每天凌晨 04:00
    echo     备份位置: %PROJECT_DIR%\backups\
    echo     自动清理: 超过 30 天的备份自动删除
) else (
    echo [!] 注册失败，请以管理员身份运行此脚本
)

echo.
pause
