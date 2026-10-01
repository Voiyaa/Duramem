@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
set "RUFF=%~dp0.venv\Scripts\ruff.exe"
set "MYPY=%~dp0.venv\Scripts\mypy.exe"

echo ============================================
echo   Duramem 提交前检查
echo ============================================
echo.

if not exist "%PY%" (
    echo [错误] 没找到 %PY%
    echo         请先创建虚拟环境并安装开发依赖：
    echo             python -m venv .venv
    echo             .venv\Scripts\python -m pip install -e ".[dev]"
    echo.
    pause
    exit /b 1
)

rem 只跑静态检查、跳过测试：check.bat lint
set "ONLY_LINT="
if /i "%~1"=="lint" set "ONLY_LINT=1"

echo [1/3] ruff（风格、import 排序、升级语法、常见 bug 模式）
"%RUFF%" check duramem tests scripts
if errorlevel 1 (
    echo.
    echo [失败] ruff 报错。多数可自动修：
    echo         .venv\Scripts\ruff check --fix duramem tests scripts
    exit /b 1
)
echo       通过
echo.

echo [2/3] mypy（跨模块类型检查，只查产品代码）
"%MYPY%" duramem
if errorlevel 1 (
    echo.
    echo [失败] mypy 报错，按上面的逐条输出修。
    exit /b 1
)
echo       通过
echo.

if defined ONLY_LINT (
    echo 静态检查全部通过（按参数跳过测试）。
    exit /b 0
)

echo [3/3] pytest（全量离线测试，约 3 分半）
"%PY%" -m pytest -q
if errorlevel 1 (
    echo.
    echo [失败] 测试未全绿，本次改动不能提交。
    exit /b 1
)

echo.
echo ============================================
echo   全部通过：可以提交
echo ============================================
exit /b 0
