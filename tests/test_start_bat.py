"""start.bat 等待段（A.18 决策二）的行为约束验证。离线，不启动真实服务/浏览器。

动态测试把真实 start.bat 的 [A.18 wait-loop] 标记段（逐字节保留原始行尾）装进
临时目录壳脚本，用真实 cmd.exe + netstat/findstr/ping 执行，"开浏览器"换成写
结果文件，服务监听用进程内 socket 模拟；仅 Windows 可跑。静态测试跨平台。
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from pathlib import Path

import pytest

START_BAT = Path(__file__).resolve().parent.parent / "start.bat"
BEGIN = b"[A.18 wait-loop begin]"
END = b"[A.18 wait-loop end]"
# A.18 约束：探测命令与文件开头逐字相同；间隔 ping -n 2；上限 30 次。
PROBE = 'netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1'

_windows_only = pytest.mark.skipif(
    os.name != "nt", reason="等待段要跑真实 cmd.exe/netstat/ping，仅 Windows"
)


def test_gbk_no_bom_line_endings_and_fixed_wait_removed():
    raw = START_BAT.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "不能有 BOM"
    text = raw.decode("gbk")  # strict：GBK 被破坏会直接失败
    assert raw.count(b"\n") == raw.count(b"\r\n"), "存在 LF-only 行"
    assert "ping -n 7" not in text, "固定等待应已移除"
    assert text.count("ping -n 5 127.0.0.1 >nul") == 1, "收尾 ping 必须保留"
    assert text.count(PROBE) == 2, "开头已运行检测与等待轮询各用一次探测"
    assert "端口 %PORT% 已在监听" in text, "已运行检测的提示文案不动"
    assert text.count("[A.18 wait-loop begin]") == 1
    assert text.count("[A.18 wait-loop end]") == 1
    # 顺序：服务启动 → 等待段 → 收尾
    assert text.index('start "Duramem Server"') < text.index("[A.18 wait-loop begin]")
    assert text.index("[A.18 wait-loop end]") < text.index("ping -n 5 127.0.0.1 >nul")


def _wait_loop_segment() -> bytes:
    """标记之间的等待段，逐字节保留原始行尾。"""
    raw = START_BAT.read_bytes()
    lines = raw.split(b"\n")
    starts = [i for i, ln in enumerate(lines) if BEGIN in ln]
    ends = [i for i, ln in enumerate(lines) if END in ln]
    assert len(starts) == 1 and len(ends) == 1 and ends[0] > starts[0]
    return b"\n".join(lines[starts[0] + 1 : ends[0]])


def test_wait_loop_uses_same_probe_ping_interval_and_30_limit():
    seg = _wait_loop_segment().decode("gbk")
    # 按整行匹配（去空白后比较）：注释里出现 ping -n 2 / 30 不算数，
    # 必须存在完整命令行，防止只留叙事注释、删掉实际逻辑也能过测试。
    lines = {re.sub(r"\s+", "", ln) for ln in seg.split("\n")}
    assert re.sub(r"\s+", "", PROBE) in lines, "探测必须是文件开头那条完整命令行"
    assert "ping-n2127.0.0.1>nul" in lines, "间隔必须是完整命令行 ping -n 2 127.0.0.1 >nul"
    assert "if%TRIES%geq30gotowait_ready_done" in lines, "上限必须是完整命令行 if %TRIES% geq 30 goto wait_ready_done"
    assert 'start ""' not in seg, "开浏览器动作在标记段之外"


def _free_port() -> int:
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", 0))
        return int(srv.getsockname()[1])
    finally:
        srv.close()


def _bindable(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _listen(port: int) -> socket.socket:
    """真实 bind+listen：让 netstat 报 LISTENING，模拟服务就绪。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    return srv


def _near_miss_pair() -> tuple[int, int]:
    """选 (target, decoy)：decoy 的十进制文本含 target 的全部数字但不紧跟冒号
    （如 2345 → 12345）。探测若漏掉冒号边界，decoy 的 LISTENING 行会被误判。
    刻意避开生产端口 8001，防止与本机真实服务冲突。
    """
    for base in (2345, 3456, 4567, 5678):
        target, decoy = base, base + 10000
        if _bindable(decoy) and _bindable(target):
            return target, decoy
    pytest.skip("找不到空闲的对照端口对")


def _make_harness(tmp_path: Path, port: int) -> tuple[Path, Path]:
    """壳脚本 = 头部（PORT/结果文件）+ 真实等待段字节 + 替换后的开浏览器动作。"""
    result = tmp_path / "browser_stub.txt"
    header = (
        b"@echo off\r\n"
        b"setlocal\r\n"
        b'set "PORT=' + str(port).encode("ascii") + b'"\r\n'
        b'set "RESULT_FILE=' + str(result).encode("gbk") + b'"\r\n'
    )
    stub = b'>>"%RESULT_FILE%" echo OPENED %TRIES%\r\nexit /b 0\r\n'
    harness = tmp_path / "wait_harness.bat"
    harness.write_bytes(header + _wait_loop_segment() + b"\n" + stub)
    return harness, result


def _run(harness: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["cmd", "/d", "/c", str(harness)],
        cwd=str(harness.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    proc.wait(timeout=15)


def _wait_result(result: Path, deadline_s: float) -> str | None:
    limit = time.monotonic() + deadline_s
    while time.monotonic() < limit:
        if result.exists():
            return result.read_text(encoding="gbk").strip()
        time.sleep(0.1)
    return None


@_windows_only
def test_opens_immediately_when_port_already_listening(tmp_path: Path):
    port = _free_port()
    srv = _listen(port)
    proc = None
    try:
        harness, result = _make_harness(tmp_path, port)
        start = time.monotonic()
        proc = _run(harness)
        opened = _wait_result(result, deadline_s=20)
        elapsed = time.monotonic() - start
        assert opened == "OPENED 0", f"第一次探测就该命中并立即打开: {opened}"
        assert elapsed < 10, f"早就绪应立即打开，实际 {elapsed:.1f}s"
    finally:
        if proc is not None:
            _kill(proc)
        srv.close()


@_windows_only
def test_opens_shortly_after_port_starts_listening(tmp_path: Path):
    port = _free_port()
    harness, result = _make_harness(tmp_path, port)
    srv = None
    proc = None
    try:
        start = time.monotonic()
        proc = _run(harness)
        time.sleep(3.0)  # 服务延后 3 秒才监听
        srv = _listen(port)
        opened = _wait_result(result, deadline_s=20)
        elapsed = time.monotonic() - start
        assert opened is not None, "端口就绪后 20s 内未打开，轮询未生效"
        tries = int(opened.split()[-1])
        assert 1 <= tries <= 20, f"应探测若干次后命中而非立即/到顶: {opened}"
        assert elapsed < 20, f"就绪后应远早于 30s 上限打开，实际 {elapsed:.1f}s"
    finally:
        if srv is not None:
            srv.close()
        if proc is not None:
            _kill(proc)


@_windows_only
def test_times_out_after_30_probes_and_opens_anyway(tmp_path: Path):
    port = _free_port()
    harness, result = _make_harness(tmp_path, port)
    start = time.monotonic()
    proc = _run(harness)
    try:
        opened = _wait_result(result, deadline_s=90)
        elapsed = time.monotonic() - start
        assert opened == "OPENED 30", f"应恰好 30 次探测到顶后打开: {opened}"
        assert 25 <= elapsed <= 85, f"超时上限应约 30s，实际 {elapsed:.1f}s"
    finally:
        _kill(proc)


@_windows_only
def test_not_fooled_by_decoy_port_missing_colon_boundary(tmp_path: Path):
    target, decoy = _near_miss_pair()
    harness, result = _make_harness(tmp_path, target)
    srv = _listen(decoy)  # 只监听 decoy：漏掉冒号边界的探测会误判为就绪
    proc = None
    try:
        proc = _run(harness)
        opened = _wait_result(result, deadline_s=6)
        assert opened is None, f"不相干端口不应被视为就绪: {opened}"
    finally:
        srv.close()
        if proc is not None:
            _kill(proc)
