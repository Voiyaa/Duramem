"""最近一次 MCP 调用快照（mcp_last_call.json，调试页「最近一次 MCP 调用」卡片）。

MCP 子进程写、REST 进程读：这份文件是两个进程之间现成的通道。这里验证
recorder 的写入语义（原子、截断、注入标志）与 Service/REST 的读取契约；
"每次 dm_ 调用都会落一条"由 test_mcp_stdio.py 端到端保证。
"""

from __future__ import annotations

import json

from duramem.mcp_server import _LastCallRecorder
from duramem.service import Service


def test_record_and_read_back(seeded: Service):
    path = seeded.settings.data_dir / "mcp_last_call.json"
    recorder = _LastCallRecorder(path)
    recorder.record(
        tool="dm_search",
        arguments={"query": "冷启动", "top_k": 5},
        payload={"results": [], "cold_start_note": "【库主备注】x"},
        db="work",
        duration_ms=42,
    )

    view = seeded.last_mcp_call()
    assert view["exists"] is True
    assert view["tool"] == "dm_search"
    assert view["db"] == "work"
    assert view["arguments"] == {"query": "冷启动", "top_k": 5}
    assert view["cold_start_injected"] is True
    assert view["duration_ms"] == 42
    assert "pid" in view and "ts" in view


def test_cold_start_flag_reflects_payload(seeded: Service):
    path = seeded.settings.data_dir / "mcp_last_call.json"
    recorder = _LastCallRecorder(path)
    recorder.record("dm_stats", {}, {"chunks_alive": 1}, "work", 1)
    assert seeded.last_mcp_call()["cold_start_injected"] is False


def test_huge_output_is_truncated_with_preview(seeded: Service):
    path = seeded.settings.data_dir / "mcp_last_call.json"
    recorder = _LastCallRecorder(path)
    huge = {"original": "x" * (_LastCallRecorder.MAX_BYTES + 1024)}
    recorder.record("dm_read_original", {"uid": "u"}, huge, "work", 5)

    view = seeded.last_mcp_call()
    output = view["output"]
    assert output["truncated"] is True
    assert output["preview"].startswith('{"original"')
    # 快照整体仍是合法 JSON 且体积受控
    assert len(path.read_text(encoding="utf-8").encode("utf-8")) < _LastCallRecorder.MAX_BYTES


def test_read_when_nothing_recorded(seeded: Service):
    assert seeded.last_mcp_call() == {"exists": False}


def test_record_is_atomic_and_alone_valid_json(seeded: Service):
    """写入必须是"整文件替换"：读到的一定是某一次完整快照，不会是半截。"""
    path = seeded.settings.data_dir / "mcp_last_call.json"
    recorder = _LastCallRecorder(path)
    for index in range(5):
        recorder.record("dm_search", {"n": index}, {"results": []}, "work", 1)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["arguments"] == {"n": index}
    assert seeded.last_mcp_call()["arguments"] == {"n": 4}
