"""MCP 集成测试：把服务作为真实的 stdio 子进程拉起来调用。

这是**最接近真实挂载路径**的测试——聊天窗口就是用这种方式挂载 Duramem 的。
前面的单元测试都直接调 Service，只有这里验证了协议层（stdio 传输、工具
schema、structuredContent）真的通。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

MESSAGES = [
    ("user", "后端报 ERR_CONN_REFUSED_0x7f 怎么回事"),
    ("assistant", "端口 BACKEND_PORT 被占了，默认 8001，改成 9000 试试"),
    ("user", "怎么看端口占用"),
    ("assistant", "netstat -ano | findstr 8001 然后 taskkill /PID <pid> /F"),
    ("user", "许可证有什么要注意的"),
    ("assistant", "避开 AGPL 和 BSL，jina-reranker-v2 是 CC-BY-NC 非商用"),
]


def _server_params(data_dir: Path, db: str) -> StdioServerParameters:
    env = {
        "DATA_DIR": str(data_dir),
        "EMBEDDING_FAKE": "true",
        "RERANK_ENABLED": "false",
        # 本测试断言的是 v3 检索契约（retrieval_mode == "hybrid"）；小库上
        # 扩散的 low_yield 信号必然触发。扩散契约在 test_links.py 验证。
        "EXPAND_ENABLED": "false",
        "DEFAULT_DB": db,
        "PYTHONPATH": str(REPO_ROOT),
        # 保证子进程不因为外部 .env 而真的去调 API
        "EMBEDDING_API_KEY": "",
        "SUMMARY_API_KEY": "",
    }
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "duramem.mcp_server", "--transport", "stdio", "--db", db],
        env=env,
        cwd=str(REPO_ROOT),
    )


def _seed(data_dir: Path, db: str) -> None:
    """先用进程内 Service 造好数据，再让 MCP 子进程连上同一个库。"""
    from duramem.config import Settings
    from duramem.models import Message
    from duramem.service import Service

    settings = Settings()
    settings.data_dir = data_dir
    settings.embedding_fake = True
    settings.rerank_enabled = False
    # 本测试断言的是 v3 检索契约（retrieval_mode == "hybrid"）；
    # 小库上扩散的 low_yield 信号必然触发。扩散本身的契约在 test_links.py 验证。
    settings.expand_enabled = False
    settings.default_db = db

    service = Service(settings)
    service.ensure_default_db()
    service.ingest(
        [
            Message("win-1", "chat-A", index, role, content,
                    speaker="我" if role == "user" else "助手")
            for index, (role, content) in enumerate(MESSAGES)
        ],
        db=db,
        auto_summarize=False,
    )
    outcome = service.summarize("win-1", "chat-A", db=db)
    assert outcome.ok, outcome.warnings
    service.close()


@pytest.mark.anyio
async def test_mcp_stdio_full_roundtrip(tmp_path):
    data_dir = tmp_path / "data"
    db = "work"
    _seed(data_dir, db)

    async with stdio_client(_server_params(data_dir, db)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ---------------------------------------------------- 工具清单
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert tools == {
                "dm_search",
                "dm_read_original",
                "dm_read_session",
                "dm_read_neighbors",
                "dm_store",
                "dm_forget",
                "dm_stats",
            }
            assert "dm_summarize" not in tools, "触发总结不应暴露给模型"

            # ---------------------------------------------------- 检索
            found = await session.call_tool(
                "dm_search", {"query": "ERR_CONN_REFUSED_0x7f 怎么解决"}
            )
            assert found.is_error is False
            payload = found.structured_content
            assert payload["retrieval_mode"] == "hybrid"
            assert payload["results"], "应检索到记忆"

            top = payload["results"][0]
            uid = top["uid"]
            assert top["has_original"] is True
            assert top["_suggestion"], "模型需要这个信号才能判断该不该看原文"
            assert "dm_read_original" in top["_suggestion"]
            # 内部排名不应泄漏给模型
            assert "vec_rank" not in top and "lex_rank" not in top

            # ---------------------------------------------------- 回查原文
            read_result = await session.call_tool(
                "dm_read_original", {"uid": uid, "mode": "window", "window": 1}
            )
            read_payload = read_result.structured_content
            assert read_payload["ok"] is True
            assert read_payload["original"].strip()
            assert read_payload["tokens"] > 0
            # 刚才的 dm_search 开了新一轮，所以这是本轮第一次回查：
            # 累计量应恰好等于本次取回量。计数只用于报告，不设上限。
            assert read_payload["tokens_this_round"] == read_payload["tokens"]
            assert "ERR_CONN_REFUSED_0x7f" in read_payload["original"] or "端口" in read_payload["original"]

            # ---------------------------------------------------- 相邻切片
            neighbours = await session.call_tool(
                "dm_read_neighbors", {"uid": uid, "before": 1, "after": 1}
            )
            neighbours_payload = neighbours.structured_content
            assert neighbours_payload["ok"] is True
            assert any(item["current"] for item in neighbours_payload["neighbors"])

            # ---------------------------------------------------- 统计
            # dm_stats 是给模型的精简投影：核心数字在，管理页专用的字段不在
            stats = (await session.call_tool("dm_stats", {})).structured_content
            assert stats["chunks_alive"] >= 1
            assert stats["vectors"] >= 1
            assert stats["sessions_summarized"] >= 1
            assert "latest_summary_at" in stats
            for admin_field in ("index", "cursors", "size_bytes", "wal_bytes",
                                "rerank_model", "summary_truncated", "active_source"):
                assert admin_field not in stats, f"{admin_field} 不该发给模型"

            # ---------------------------------------------------- 未知 uid 不抛错
            missing = await session.call_tool("dm_read_original", {"uid": "nope"})
            assert missing.is_error is False
            assert missing.structured_content["ok"] is False
            assert missing.structured_content["warnings"]

            # ---------------------------------------------------- 写入与删除
            stored = await session.call_tool(
                "dm_store",
                {"summary_text": "用户偏好用 sqlite-vec 而非 zvec", "keywords": ["sqlite-vec"]},
            )
            stored_payload = stored.structured_content
            assert stored_payload["ok"] is True
            new_uid = stored_payload["uid"]

            deleted = await session.call_tool("dm_forget", {"uid": new_uid})
            assert deleted.structured_content["ok"] is True

            # 最近一次调用快照：MCP 子进程落盘，前端调试页读的就是它
            last_call = json.loads(
                (data_dir / "mcp_last_call.json").read_text(encoding="utf-8")
            )
            assert last_call["tool"] == "dm_forget"
            assert last_call["db"] == "work"
            # SDK 会把带默认值的参数一起传进来（db: None 等），按关键键断言
            assert last_call["arguments"]["uid"] == new_uid
            assert last_call["cold_start_injected"] is False


@pytest.mark.anyio
async def test_mcp_stdio_isolates_databases_by_launch_arg(tmp_path):
    """stdio 模式下每个窗口是独立进程，`--db` 就是最干净的库选择方式。"""
    data_dir = tmp_path / "data"
    _seed(data_dir, "work")

    async with stdio_client(_server_params(data_dir, "work")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            stats = (await session.call_tool("dm_stats", {})).structured_content
            assert stats["db"] == "work"
            assert stats["chunks_alive"] >= 1

def _seed_library(data_dir: Path, db: str, messages=MESSAGES) -> None:
    """造一个指定名字的库（已存在就复用），并写一次切片。"""
    from duramem.config import Settings
    from duramem.models import Message
    from duramem.service import Service

    settings = Settings()
    settings.data_dir = data_dir
    settings.embedding_fake = True
    settings.rerank_enabled = False

    service = Service(settings)
    if not service.registry.exists(db):
        service.create_database(db)
    service.ingest(
        [
            Message("win-1", "chat-A", index, role, content,
                    speaker="我" if role == "user" else "助手")
            for index, (role, content) in enumerate(messages)
        ],
        db=db,
        auto_summarize=False,
    )
    outcome = service.summarize("win-1", "chat-A", db=db)
    assert outcome.ok, outcome.warnings
    service.close()


@pytest.mark.anyio
async def test_active_db_pointer_drives_and_guards_writes(tmp_path):
    """当前记忆库：默认落到指针指的库，写入只允许进它，换指针后下一次检索就切过去。

    这三条对应设计上的取舍：
    - 「下一轮生效」在协议里没有轮边界，落点是**一轮里最早的检索**（见 mcp_server）。
    - 写入显式指到别的库时**拒绝**而不是改写目标——静默改写会让模型以为写进了 A。
    """
    data_dir = tmp_path / "data"
    _seed_library(data_dir, "work")
    _seed_library(data_dir, "life")   # 少于 4 条对话内容会被"太短"跳过，所以给全量

    from duramem import active_db

    active_db.write(data_dir, "work", by="test")

    async with stdio_client(_server_params(data_dir, "work")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ① 默认落到指针指的库，并回显它是从哪来的
            stats = (await session.call_tool("dm_stats", {})).structured_content
            assert stats["db"] == "work"
            assert stats["active_db"] == "work"

            # ② 写入显式指到别的库 → 拒绝，并说清当前库是哪个
            refused = (
                await session.call_tool(
                    "dm_store", {"summary_text": "不该写进 life", "db": "life"}
                )
            ).structured_content
            assert refused["ok"] is False
            assert "只允许进当前记忆库" in refused["error"]
            assert "work" in refused["error"]

            # ③ 不带 db 的写入进当前库
            stored = (
                await session.call_tool("dm_store", {"summary_text": "写进当前库的记忆"})
            ).structured_content
            assert stored["ok"] is True
            assert stored["db"] == "work"

            # ④ 界面换了指针（这里直接写文件，等价于前端点「设为记忆库」）
            active_db.write(data_dir, "life", by="ui")

            # 下一次检索（= 一轮的边界）就切过去
            found = await session.call_tool("dm_search", {"query": "端口 被占"})
            payload = found.structured_content
            assert payload["results"], "life 库自己也有切片"
            assert all(item["db"] == "life" for item in payload["results"])

            stats_after = (await session.call_tool("dm_stats", {})).structured_content
            assert stats_after["db"] == "life"
            assert stats_after["active_db"] == "life"


def _set_cold_start(data_dir: Path, db: str, note: str, enabled: bool) -> None:
    """以独立进程的身份写某库的冷启动配置（等价于前端在库管理页保存）。

    配置存在库自己的 db_meta 里：测试里这里写一次，MCP 子进程每次工具调用
    都会重新读库——跨进程可见性正是"绑定到库"这条设计要验证的东西。
    """
    from duramem.config import Settings
    from duramem.service import Service

    settings = Settings()
    settings.data_dir = data_dir
    settings.embedding_fake = True
    service = Service(settings)
    service.set_cold_start(db, note=note, enabled=enabled)
    service.close()


@pytest.mark.anyio
async def test_mcp_stdio_cold_start_note_first_call_only(tmp_path):
    """库主留言（存库内）：首次调用附带、同进程第二次不再有；未配置（默认）则完全不出现。

    这条同时验证装饰器注册链路：functools.wraps 必须保住签名与注解，
    否则 SDK 的 schema 内省会在 list_tools/call_tool 上当场炸掉。
    """
    data_dir = tmp_path / "data"
    db = "work"
    _seed(data_dir, db)
    _set_cold_start(data_dir, db, note="项目代号 K3，检索时请带上。", enabled=True)

    async with stdio_client(_server_params(data_dir, db)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            stats = (await session.call_tool("dm_stats", {})).structured_content
            assert "项目代号 K3" in stats["cold_start_note"]

            # 同一进程内的第二次调用不再附带
            stats_again = (await session.call_tool("dm_stats", {})).structured_content
            assert "cold_start_note" not in stats_again


@pytest.mark.anyio
async def test_mcp_stdio_cold_start_switch_rearm(tmp_path):
    """注入开关是手动边沿触发、按库绑定：关→不注入；重新拨开→长驻子进程下一次调用再注入。

    配置由**另一个进程**（模拟前端的 Service）写进同一个库文件，
    子进程不重启就能读到——这是"绑定到库"区别于全局设置的关键。
    """
    data_dir = tmp_path / "data"
    db = "work"
    _seed(data_dir, db)
    _set_cold_start(data_dir, db, note="项目代号 K3", enabled=True)

    async with stdio_client(_server_params(data_dir, db)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            def _last_call():
                return json.loads(
                    (data_dir / "mcp_last_call.json").read_text(encoding="utf-8")
                )

            stats = (await session.call_tool("dm_stats", {})).structured_content
            assert "项目代号 K3" in stats["cold_start_note"]
            # 快照只保留最近一次调用，标志必须在下下一次调用前检查
            assert _last_call()["cold_start_injected"] is True

            assert "cold_start_note" not in (
                await session.call_tool("dm_stats", {})
            ).structured_content
            assert _last_call()["cold_start_injected"] is False

            # 前端把开关关掉：完全不注入
            _set_cold_start(data_dir, db, note="项目代号 K3", enabled=False)
            assert "cold_start_note" not in (
                await session.call_tool("dm_stats", {})
            ).structured_content

            # 重新拨开：边沿，下一次调用再注入一次
            _set_cold_start(data_dir, db, note="项目代号 K3", enabled=True)
            stats_rearmed = (await session.call_tool("dm_stats", {})).structured_content
            assert "项目代号 K3" in stats_rearmed["cold_start_note"]
            assert "cold_start_note" not in (
                await session.call_tool("dm_stats", {})
            ).structured_content
