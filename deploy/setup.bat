@echo off
chcp 65001 >nul
title 凯尔希状态机 - 一键安装

:: 国内镜像（pypi.org 在部分机房访问不稳定时可改用此处）
set "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
set "PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn"

echo ========================================
echo   凯尔希状态机 - 服务器环境安装
echo ========================================
echo.

:: ── 检查 Python ──
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [!] 未检测到 Python。
    echo     在线环境：将打开官方安装包下载页，安装时请勾选 "Add to PATH"
    echo     离线环境：请从可联网电脑下载 python-3.11.x-amd64.exe 拷贝到本机安装后再运行本脚本
    start https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
    echo.
    echo [!] 安装完 Python 后请关闭此窗口，重新运行 setup.bat
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do echo [OK] %%i
where python
if exist "%SystemRoot%\py.exe" (
    echo [OK] 检测到 py 启动器
)

:: ── 定位项目根目录（deploy 的上一级） ──
set "PROJECT_DIR=%~dp0.."
pushd "%PROJECT_DIR%"
set "PROJECT_DIR=%CD%"
popd
echo [OK] 项目目录: %PROJECT_DIR%

:: ── 创建虚拟环境 ──
if not exist "%PROJECT_DIR%\.venv" (
    echo [..] 创建虚拟环境...
    python -m venv "%PROJECT_DIR%\.venv"
    if %ERRORLEVEL% NEQ 0 (
        echo [!] 虚拟环境创建失败
        pause
        exit /b 1
    )
    echo [OK] 虚拟环境已创建
) else (
    echo [OK] 虚拟环境已存在
)

set "VENV_DIR=%PROJECT_DIR%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
"%VENV_PY%" --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [!] 检测到现有 .venv 已损坏或绑定了旧 Python 路径，正在重建...
    if exist "%VENV_DIR%" (
        rmdir /s /q "%VENV_DIR%"
        if exist "%VENV_DIR%" (
            echo [!] 无法删除旧 .venv（可能被占用）
            echo     请先关闭所有正在运行的 start.bat / Python 进程后重试
            pause
            exit /b 1
        )
    )
    echo [..] 尝试使用当前 python 重建虚拟环境...
    python -m venv "%VENV_DIR%"
    if %ERRORLEVEL% NEQ 0 (
        echo [!] 使用 python -m venv 重建失败，尝试 py 启动器...
        if exist "%SystemRoot%\py.exe" (
            py -3.11 -m venv "%VENV_DIR%"
            if %ERRORLEVEL% NEQ 0 (
                py -3.10 -m venv "%VENV_DIR%"
            )
        )
        if %ERRORLEVEL% NEQ 0 (
            echo [!] 虚拟环境重建失败
            echo     建议检查：
            echo     1. Python 是否完整安装（非 embeddable 版本）
            echo     2. 是否安装了 venv 组件
            echo     3. 项目目录是否有写入权限：%PROJECT_DIR%
            echo.
            echo     你也可以手动执行以下命令查看具体错误：
            echo     python -m venv "%VENV_DIR%"
            echo     py -3.10 -m venv "%VENV_DIR%"
            pause
            exit /b 1
        )
    )
    echo [OK] 虚拟环境已重建
)

:: ── 安装依赖（若存在 deploy\wheels 则离线安装，否则走 PyPI）──
:: 已从本机复制完整 .venv 时：在 deploy 下建空文件 skip_pip.txt，或先执行 set KELSEY_SKIP_PIP=1 再运行本脚本，可跳过 pip
if /i "%KELSEY_SKIP_PIP%"=="1" goto skip_pip
if exist "%PROJECT_DIR%\deploy\skip_pip.txt" goto skip_pip

set "WHEELS_DIR=%PROJECT_DIR%\deploy\wheels"
set "TRY_ONLINE=1"
dir /b "%WHEELS_DIR%\*.whl" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [..] 检测到 deploy\wheels，优先使用离线安装...
    "%VENV_PY%" -m pip install --no-index --find-links="%WHEELS_DIR%" -r "%PROJECT_DIR%\requirements.txt" -q
    if %ERRORLEVEL% EQU 0 (
        set "TRY_ONLINE=0"
    ) else (
        echo [!] 离线安装失败，可能是 wheels 与当前 Python 版本不匹配（例如 cp311 vs cp310）
        echo [..] 将自动尝试使用清华镜像在线安装...
    )
) else (
    echo [..] 未检测到可用离线 wheel，改为在线安装...
)

if /i "%TRY_ONLINE%"=="1" (
    echo [..] 安装 Python 依赖（使用清华镜像: %PIP_INDEX_URL%）...
    "%VENV_PY%" -m pip install -r "%PROJECT_DIR%\requirements.txt" -i "%PIP_INDEX_URL%" --trusted-host "%PIP_TRUSTED_HOST%" -q
)
if %ERRORLEVEL% NEQ 0 (
    echo [!] 依赖安装失败
    echo     若服务器无外网：请在可联网电脑运行 deploy\fetch_wheels.bat，将生成的 deploy\wheels 一并打包后再运行 setup.bat
    echo     并确保下载 wheels 的 Python 主次版本与服务器一致（例如都为 3.10 或都为 3.11）
    echo     若已整包复制 .venv：可在 deploy 目录新建空文件 skip_pip.txt 后重新运行本脚本以跳过 pip
    pause
    exit /b 1
)
echo [OK] 依赖安装完成
goto after_pip

:skip_pip
echo [OK] 已跳过 pip（沿用现有 .venv）。若启动报错，请检查 .venv\pyvenv.cfg 中 home 是否指向本机 Python 安装目录

:after_pip

:: ── 创建 data 目录 ──
if not exist "%PROJECT_DIR%\data" (
    mkdir "%PROJECT_DIR%\data"
    echo [OK] data 目录已创建
)

:: ── 创建备份目录 ──
if not exist "%PROJECT_DIR%\backups" (
    mkdir "%PROJECT_DIR%\backups"
    echo [OK] backups 目录已创建
)

:: ── 防火墙规则（8000端口入站） ──
netsh advfirewall firewall show rule name="Kelsey-MCP-8000" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [..] 添加防火墙入站规则（端口 8000）...
    netsh advfirewall firewall add rule name="Kelsey-MCP-8000" dir=in action=allow protocol=TCP localport=8000 >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        echo [OK] 防火墙规则已添加
    ) else (
        echo [!] 防火墙规则添加失败（可能需要管理员权限）
        echo     请手动在 Windows 防火墙中放行 TCP 8000 端口
    )
) else (
    echo [OK] 防火墙规则已存在
)

echo.
echo ========================================
echo   安装完成！
echo ========================================
echo.
echo   下一步：
echo   1. 编辑 config.yaml 填入你的 LLM API 信息
echo   2. 双击 start.bat 启动服务
echo   3. 双击 autostart.bat 注册开机自启
echo.
pause
