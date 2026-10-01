@echo off
setlocal
cd /d "%~dp0"

set "PORT=8001"
if not "%~1"=="" set "PORT=%~1"

echo ============================================
echo   Duramem 停止脚本
echo ============================================
echo.

set "FOUND="
rem 只按端口找进程，不按进程名杀。
rem ZCode 会把 Duramem 作为 MCP 子进程拉起（同样是 duramem.exe），
rem 按进程名杀会连带干掉那些子进程，把正在用的会话搞坏。
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    if not "%%p"=="0" (
        echo 正在停止 PID %%p（监听 %PORT% 的进程）...
        taskkill /F /PID %%p >nul 2>&1
        if errorlevel 1 (
            echo   [失败] 可能权限不足，试试以管理员身份运行。
        ) else (
            echo   [完成]
        )
        set "FOUND=1"
    )
)

if not defined FOUND (
    echo 端口 %PORT% 上没有在运行的服务。
    echo 如果服务确实在跑，可能是改了端口，试试：stop.bat 端口号
)

echo.
pause
exit /b 0
