@echo off
setlocal
cd /d "%~dp0"

set "PORT=8001"
set "DATA_DIR=%~dp0data"
set "DM=%~dp0.venv\Scripts\duramem.exe"

echo ============================================
echo   Duramem 记忆服务
echo ============================================
echo.

if not exist "%DM%" (
    echo [错误] 没找到 %DM%
    echo.
    echo 请先创建虚拟环境并安装依赖：
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -e .
    echo.
    pause
    exit /b 1
)

rem 延时用 ping 而不是 timeout：timeout 在某些环境（比如从 Git Bash 调用）
rem 会被 coreutils 的同名命令截走，报 invalid time interval。ping 到处都在。
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [提示] 端口 %PORT% 已在监听，服务应该已经在运行。
    echo        正在打开界面。如果打不开，先运行 stop.bat 再重试。
    echo.
    start "" "http://127.0.0.1:%PORT%"
    ping -n 4 127.0.0.1 >nul
    exit /b 0
)

echo 数据目录：%DATA_DIR%
echo 监听端口：%PORT%
echo.
echo 服务会在另一个窗口里运行。关掉那个窗口即停止，
echo 或者随时双击 stop.bat 停止。
echo.

rem 不加 /b：让服务在独立窗口里跑，日志可见，关窗即停
start "Duramem Server" cmd /k ""%DM%" serve --data-dir "%DATA_DIR%" --port %PORT%"

echo 等待服务起来...
ping -n 7 127.0.0.1 >nul
start "" "http://127.0.0.1:%PORT%"
echo 已在浏览器打开 http://127.0.0.1:%PORT%
echo.
ping -n 5 127.0.0.1 >nul
exit /b 0
