@echo off
chcp 65001 >nul

set "PROJECT_DIR=%~dp0.."
pushd "%PROJECT_DIR%"
set "PROJECT_DIR=%CD%"
popd

set "DB_PATH=%PROJECT_DIR%\data\kelsey.db"
set "BACKUP_DIR=%PROJECT_DIR%\backups"

if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"

:: 生成时间戳文件名
for /f "tokens=1-3 delims=/ " %%a in ('date /t') do set "D=%%a%%b%%c"
for /f "tokens=1-2 delims=: " %%a in ('time /t') do set "T=%%a%%b"
set "TIMESTAMP=%D%_%T%"

if not exist "%DB_PATH%" (
    echo [!] 数据库文件不存在: %DB_PATH%
    exit /b 1
)

copy "%DB_PATH%" "%BACKUP_DIR%\kelsey_%TIMESTAMP%.db" >nul
if %ERRORLEVEL% EQU 0 (
    echo [OK] 备份完成: backups\kelsey_%TIMESTAMP%.db
) else (
    echo [!] 备份失败
    exit /b 1
)

:: 清理超过 30 天的旧备份
forfiles /p "%BACKUP_DIR%" /m "kelsey_*.db" /d -30 /c "cmd /c del @path" >nul 2>&1

exit /b 0
