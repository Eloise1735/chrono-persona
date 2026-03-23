@echo off
chcp 65001 >nul
title 凯尔希状态机 - 下载离线依赖包

set "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
set "PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn"

echo ========================================
echo   预下载 pip 依赖到 deploy\wheels
echo   （清华镜像；请在可联网的 Windows 电脑上运行，Python 版本尽量与服务器一致）
echo ========================================
echo.

set "PROJECT_DIR=%~dp0.."
pushd "%PROJECT_DIR%"
set "PROJECT_DIR=%CD%"
popd

python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [!] 未检测到 Python，请先安装 Python 3.11 x64 并加入 PATH
    pause
    exit /b 1
)

set "WHEELS=%~dp0wheels"
if not exist "%WHEELS%" mkdir "%WHEELS%"

echo [..] 正在下载 wheel 到:
echo     %WHEELS%
echo.

python -m pip download -r "%PROJECT_DIR%\requirements.txt" -d "%WHEELS%" -i "%PIP_INDEX_URL%" --trusted-host "%PIP_TRUSTED_HOST%"
if %ERRORLEVEL% NEQ 0 (
    echo [!] 下载失败
    pause
    exit /b 1
)

echo.
echo [OK] 完成。将整份项目（含 deploy\wheels）打包拷贝到无网服务器后运行 setup.bat 即可离线安装依赖。
echo.
pause
