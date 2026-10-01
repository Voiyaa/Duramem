"""MCP 服务：stdio + streamable-http 双模式。

设计文档 §9。要点：

- 工具统一 `dm_` 前缀。宿主会把所有 MCP server 的工具平铺给模型，
  跨 server 的工具名冲突是实际会发生的问题。
- **`dm_summarize` 不进 MCP 工具集**。给模型触发总结的能力会让它随意调用；
  总结只由采集侧和前端触发。
- 返回 dict，由 SDK 同时产出 content blocks（文本）与 structuredContent——
  裸 JSON 字符串在部分客户端（如 Claude Desktop）解析会出问题。
- 工具描述本身就是给模型的判断依据。R5（模型自主决定是否看原文）能不能成立，
  很大程度上取决于 `dm_search` 的描述有没有告诉它"摘要可能不够用"。
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from duramem import __version__
from duramem.config import get_settings
from duramem.service import DuramemError, Service
from duramem.store.registry import now_iso

SERVER_NAME = "duramem"

_READ_MODE = Literal["quote", "window", "full"]
_SESSION_DETAIL = Literal["abstract", "overview"]
_READ_DETAIL = Literal["minimal", "standard", "full"]

MANUAL_STORE_LIMIT = 20
MANUAL_STORE_WINDOW_SECONDS = 300


class _ColdStartNote:
    """库主留言的冷启动注入器（库管理页按库配置）。

    每个记忆库可以有自己的固定说明（note）与注入开关，存**库自己的 db_meta**
    ——绑定随库文件走，复制/快照/归档往返都带着。模型调用 dm_ 工具时，注入器
    按**本次调用实际生效的库**取配置，随结果附带。为什么不常驻注入：MCP 没有
    注入通道，工具结果是唯一载体；为什么不做成独立工具：模型冷启动时不知道
    该调它——自举困境。note 留空 = 该库彻底关闭，响应与从前逐字节一致。

    注入时机是**手动边沿触发，按库记状态**：某库的配置"开着、且与上一次见到
    的不一样"（进程刚启动即见开、开关关→开、或 note 内容改了）时，下一次以该
    库为目标的工具调用注入一次，之后静默。改 note 内容也算一次新边沿——
    "改一下留言就重新注入"是最顺手的重臂方式。是否注入、何时注入完全由用户
    手动掌握。
    """

    def __init__(self, service: Service) -> None:
        self._service = service
        # 每库上次见到的 (enabled, note)。缺 = 该库还没被见过（第一次见即边沿）
        self._seen: dict[str, tuple[bool, str]] = {}

    def apply(self, db: str, payload: dict[str, Any]) -> dict[str, Any]:
        """按库取配置，按需把留言附到工具结果上。取不到配置时原样返回。

        留言键刻意放在返回体**第一个**：模型按顺序读 JSON，先读到框架说明、
        再读检索结果——先有"这段内容是什么、该怎么用"，后有内容本身。
        """
        try:
            view = self._service.cold_start_view(db)
        except Exception:  # noqa: BLE001 - 库打不开/不存在时注入必须保持沉默
            return payload
        note = str(view.get("note") or "").strip()
        if not note:
            return payload
        enabled = bool(view.get("enabled"))
        last = self._seen.get(db)
        fire = enabled and (last is None or last[0] is not True or last[1] != note)
        self._seen[db] = (enabled, note)
        if not fire:
            return payload
        # 复制而不是原地改：调用方（工具处理函数）的返回值不该被注入器污染
        out: dict[str, Any] = {
            "cold_start_note": (
                f"【库主备注】以下是记忆库「{db}」的固定说明，"
                "由用户写下、在调用该库的记忆工具时附带：\n" + note
            )
        }
        out.update(payload)
        return out


class _LastCallRecorder:
    """把最近一次 MCP 工具调用的输入与输出落到 `data/mcp_last_call.json`。

    供前端「调试」页回答"宿主模型最近一次到底发了什么、看到了什么"——检索
    调试面板只能重放你自己发的查询，看不见模型的真实调用。MCP 子进程与
    REST 进程是两个进程，落文件是它们之间现成的通道（与 active_db.json
    同一模式、方向相反：那次是 UI 写 MCP 读，这次是 MCP 写 UI 读）。多窗口
    并存时最后写入者胜出——"最近一次"本来就是这个语义。记录永远不影响
    工具调用本身：调用方对写失败静默。
    """

    # 超大输出整体截断只留预览（dm_read_original 的 full 模式可能很大）——
    # 这份文件是"最近一次"的快照，不是日志。
    MAX_BYTES = 512 * 1024

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def record(
        self,
        tool: str,
        arguments: dict[str, Any],
        payload: dict[str, Any],
        db: str | None,
        duration_ms: int,
    ) -> None:
        entry: dict[str, Any] = {
            "tool": tool,
            "db": db,
            "arguments": arguments,
            "cold_start_injected": isinstance(payload, dict) and "cold_start_note" in payload,
            "duration_ms": duration_ms,
            "ts": now_iso(),
            "pid": os.getpid(),
            "output": payload,
        }
        if len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) > self.MAX_BYTES:
            entry["output"] = {
                "truncated": True,
                "preview": json.dumps(payload, ensure_ascii=False)[:2000],
            }
        # 原子替换（AGENTS.md 铁律）：REST 进程可能正在读这个文件
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)


class _RateLimiter:
    """给 `dm_store` 限流。模型会滥用一切能写库的工具。"""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._events: dict[str, list[float]] = {}

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        events = [t for t in self._events.get(key, []) if now - t < self.window]
        if len(events) >= self.limit:
            self._events[key] = events
            return False, 0
        events.append(now)
        self._events[key] = events
        return True, self.limit - len(events)


def build_server(service: Service | None = None) -> MCPServer:
    svc = service or Service(get_settings())
    limiter = _RateLimiter(MANUAL_STORE_LIMIT, MANUAL_STORE_WINDOW_SECONDS)
    cold_start = _ColdStartNote(svc)
    recorder = _LastCallRecorder(Path(svc.settings.data_dir) / "mcp_last_call.json")

    def _cold(fn):
        """把当前库的冷启动留言附到工具结果上。

        叠在 `@server.tool(...)` **之下**：注册进 SDK 的是包装后的函数。
        functools.wraps 保住 `__signature__`/`__annotations__`，SDK 的
        schema 内省（inspect.signature + get_type_hints）看到的仍是原函数。

        生效库的解析**在处理函数跑完之后**做：显式传了 `db` 用显式值；没传则
        用轮状态里已定的库（dm_search/dm_stats 在处理函数里已把轮边界刷新过），
        轮状态也为空时才落到当前指针。刻意**不调 `_turn_db(refresh=True)`**——
        注入器不得打断"一轮里最早的检索才算轮边界"的语义。解析或取配置失败
        一律原样返回，注入永远不影响工具结果本身。

        同一组装点顺带把这次调用（工具名 / 参数 / 输出 / 生效库 / 耗时 /
        是否注入了冷启动留言）记进 mcp_last_call.json 供前端调试页展示。
        """

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            payload = fn(*args, **kwargs)
            db: str | None = None
            try:
                db = str(kwargs.get("db") or turn["db"] or svc.ensure_default_db())
            except Exception:  # noqa: BLE001 - 解析不出库就没有"该用谁的留言"
                db = None
            out = payload
            if db is not None:
                try:
                    out = cold_start.apply(db, payload)
                except Exception:  # noqa: BLE001 - 注入失败不该拖垮工具调用
                    out = payload
            try:
                recorder.record(
                    tool=fn.__name__,
                    arguments=kwargs,
                    payload=out,
                    db=db,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            except Exception:  # noqa: BLE001 - 记录失败不影响工具调用
                pass
            return out

        return wrapper
    # 本进程"当前轮"的记忆库。
    #
    # "切库下一轮生效"在 MCP 协议里没有现成的边界信号——宿主不告诉服务端
    # "这是一轮的开始"。这里的做法是：把**一轮里最早的检索/看状态**当作边界，
    # 那时才重读前端写下的指针；同一轮里随后的回查与写入继续用这一轮已定的库。
    # 于是"你切库"不会把一轮中途的记忆来源换掉，下一轮才整体切过去。
    turn: dict[str, Any] = {"db": None}

    def _turn_db(refresh: bool = False) -> str:
        if refresh or turn["db"] is None:
            turn["db"] = svc.ensure_default_db()
        return str(turn["db"])

    def _active_or_refuse(explicit: str | None) -> tuple[str | None, dict[str, Any] | None]:
        """写入路径的准入：只允许进当前记忆库。

        显式指到别的库时不静默改写——那会让模型以为写进了 A，实际落在 B。
        直接拒绝，并告诉它当前库是哪个。
        """
        active = _turn_db()
        if explicit and explicit != active:
            return None, {
                "ok": False,
                "error": (
                    f"写入只允许进当前记忆库（{active}）。"
                    f"要写进「{explicit}」，请先在界面上把它设为当前记忆库。"
                ),
                "active_db": active,
            }
        return active, None

    server = MCPServer(
        name=SERVER_NAME,
        title="Duramem 记忆服务",
        version=__version__,
        instructions=(
            "Duramem 是用户的长期记忆库。记忆组织成一棵两级的树，由粗到细：\n"
            "- **会话简介**：一段会话的一句话定位。dm_search 命中会话时随结果返回，\n"
            "  用于「我们接着上次那个来」这类没有关键词的冷启动。\n"
            "- **会话概览**：一段会话的整体脉络，含「覆盖度」与「导航」两段。导航段\n"
            "  用「想知道什么 → uid」的形式指路，告诉你需要细节时该读哪一条切片。\n"
            "  用 dm_read_session 取。\n"
            "- **切片摘要**：每条记忆一段，dm_search 返回的就是它。便宜，用于判断\n"
            "  相关性，也是每轮自动注入的唯一一层。\n"
            "- **切片原文**：切片背后逐字的对话。用 dm_read_original 取。\n\n"
            "使用原则：\n"
            "1. 回答涉及用户过往经历、偏好、已做决定的问题前，先调用 dm_search。\n"
            "2. 想知道「这段会话整体在讲什么」或「该看哪一条」时，用 dm_read_session。\n"
            "3. 摘要、简介与概览**都可能不足以回答细节问题**。当问题涉及具体数值、\n"
            "   原话、错误码、文件路径、版本号、参数名时，**应当**用 dm_read_original\n"
            "   查看原文，不要凭摘要或概览推测——它们是二次加工的产物，一定会改写措辞。\n"
            "4. 只读最可能改变你下一步行动的那 1-3 条切片，不要为了保险把命中的\n"
            "   全部读一遍。\n"
            "5. dm_search 的每条结果都带 _suggestion；has_original 为 true 表示能取到\n"
            "   原文，session_id 则可用于取会话概览。\n"
            "6. 不要凭空断言记忆里没有的事。没检索到就说没检索到。"
        ),
    )

    # ================================================================ 检索

    @server.tool(
        name="dm_search",
        description=(
            "检索长期记忆，返回相关的记忆摘要切片。\n\n"
            "何时用：需要回忆用户说过的话、做过的决定、项目约定、个人偏好时。\n"
            "返回字段：uid（后续回查原文要用）、title、summary（摘要）、score、"
            "hit_count、has_original、msg_range、_suggestion。\n"
            "results 是查询的直接命中；expanded（若出现）是图扩散沿记忆间链接"
            "补充的相关切片（origin=expand），与直接命中相关但查询词没够到，"
            "一并考虑，但以直接命中为主。\n\n"
            "重要：返回的是**摘要**，不是原文。如果摘要不足以回答，"
            "用 dm_read_original 取原文。检索结果推导不出答案时，明确说明没找到，不要编造。"
        ),
    )
    @_cold
    def dm_search(
        query: str,
        top_k: int = 5,
        tags: list[str] | None = None,
        db: str | None = None,
        session: str = "default",
    ) -> dict[str, Any]:
        """检索记忆切片。

        Args:
            query: 检索词。专有名词、错误码、文件名直接用原词，效果最好。
            top_k: 返回条数。
            tags: 按标签过滤，可选。
            db: 指定库；不传则用当前记忆库（前端选定的那个）。
            session: 会话标识，用于回查额度分轮。
        """
        try:
            # 检索是一轮里最早的调用 → 这里才重读"当前记忆库"指针
            result = svc.search(
                query,
                db=db or _turn_db(refresh=True),
                top_k=top_k,
                tags=tags,
                session=session,
                collect_debug=False,
            )
            return result.to_mcp_payload()
        except DuramemError as exc:
            return {"results": [], "error": str(exc), "retrieval_mode": "error"}
        except Exception as exc:  # noqa: BLE001
            return {"results": [], "error": f"检索失败：{exc}", "retrieval_mode": "error"}

    # ================================================================ 回查

    @server.tool(
        name="dm_read_original",
        description=(
            "查看某条记忆切片背后的原文。\n\n"
            "何时用：摘要缺少你需要的细节时——具体数值、原话措辞、错误码、"
            "命令、文件路径、参数名、时间点。**不要凭摘要推测这些细节。**\n\n"
            "mode 的粒度选择：\n"
            "- quote：只取该切片对应的精确原文区间，最省（默认）\n"
            "- window：在区间基础上向前后各扩展 window 条消息，适合需要上下文时\n"
            "- full：取该切片关联的全部原文\n\n"
            "detail：minimal 只给约 500 token（截在句末）；standard 完整；"
            "full 完整且不做单次截断。\n\n"
            "取回量不设上限，读多少给多少；返回体会报告本轮已取回多少 token"
            "（tokens_this_round）供你自查。\n\n"
            "只读最可能改变你下一步行动的那几条，不要为了保险全部读一遍。"
        ),
    )
    @_cold
    def dm_read_original(
        uid: str,
        mode: _READ_MODE = "quote",
        window: int = 1,
        detail: _READ_DETAIL = "standard",
        db: str | None = None,
        session: str = "default",
    ) -> dict[str, Any]:
        """回查切片原文。

        Args:
            uid: dm_search 返回的 uid。
            mode: quote / window / full。
            window: mode=window 时向前后各扩展多少条消息。
            detail: minimal / standard / full。
            db: 指定库（跨库检索时结果里会给出）。
            session: 会话标识，用于回查额度分轮。
        """
        try:
            from duramem.reader import (
                DETAIL_FULL,
                DETAIL_MINIMAL,
                DETAIL_STANDARD,
                MODE_FULL,
                MODE_QUOTE,
                MODE_WINDOW,
            )

            detail_map = {
                "minimal": DETAIL_MINIMAL,
                "standard": DETAIL_STANDARD,
                "full": DETAIL_FULL,
            }
            mode_map = {"quote": MODE_QUOTE, "window": MODE_WINDOW, "full": MODE_FULL}

            result = svc.read_original(
                uid,
                db=db,
                mode=mode_map.get(mode, MODE_QUOTE),
                window=window,
                detail=detail_map.get(detail, DETAIL_STANDARD),
                session=session,
            )
            return result.to_payload()
        except DuramemError as exc:
            return {"ok": False, "uid": uid, "warnings": [str(exc)], "original": ""}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "uid": uid, "warnings": [f"回查失败：{exc}"], "original": ""}

    @server.tool(
        name="dm_read_session",
        description=(
            "查看一段会话的整体概览。\n\n"
            "何时用：\n"
            "- 想知道「这段会话整体在讲什么」——切片摘要给不出脉络时；\n"
            "- 命中了好几条同一会话的切片，想知道它们的相互关系；\n"
            "- 需要导航：概览里的「导航」段用「想知道什么 → uid」指路，"
            "据此再用 dm_read_original 取具体那条的原文。\n\n"
            "detail：abstract 只给会话简介（约 256 token，便宜的探路）；"
            "overview（默认）给摘要 + 完整概览。\n\n"
            "注意：概览是**二次加工**的产物，会改写措辞。需要原话、"
            "具体数值、错误码时必须转用 dm_read_original。"
        ),
    )
    @_cold
    def dm_read_session(
        session_id: str,
        detail: _SESSION_DETAIL = "overview",
        db: str | None = None,
        session: str = "default",
    ) -> dict[str, Any]:
        """读取会话概览。

        Args:
            session_id: dm_search 命中里的 session_id。
            detail: abstract（仅简介）/ overview（简介 + 概览，默认）。
            db: 指定库；不传则用当前记忆库。
            session: 会话标识，用于取回量分轮。
        """
        try:
            from duramem.reader import SESSION_DETAIL_ABSTRACT, SESSION_DETAIL_OVERVIEW

            detail_map = {"abstract": SESSION_DETAIL_ABSTRACT, "overview": SESSION_DETAIL_OVERVIEW}
            result = svc.read_session(
                session_id,
                detail=detail_map.get(detail, SESSION_DETAIL_OVERVIEW),
                db=db or _turn_db(),
                session=session,
            )
            return result.to_payload()
        except DuramemError as exc:
            return {"ok": False, "session_id": session_id, "warnings": [str(exc)]}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "session_id": session_id,
                "warnings": [f"读取会话概览失败：{exc}"],
            }

    @server.tool(
        name="dm_read_neighbors",
        description=(
            "查看与某条切片相邻的切片（同一次对话里前后的记忆）。\n\n"
            "何时用：当前切片的摘要看起来相关但缺少前因后果，想知道前后还记了什么。\n"
            "返回相邻切片的摘要列表（不是原文），可再对其中某条调用 dm_read_original。"
        ),
    )
    @_cold
    def dm_read_neighbors(
        uid: str,
        before: int = 1,
        after: int = 1,
        db: str | None = None,
    ) -> dict[str, Any]:
        """取相邻切片。

        Args:
            uid: 基准切片的 uid。
            before: 向前取几条。
            after: 向后取几条。
            db: 指定库。
        """
        try:
            return svc.read_neighbors(uid, db=db, before=before, after=after)
        except DuramemError as exc:
            return {"ok": False, "warnings": [str(exc)], "neighbors": []}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "warnings": [f"取相邻切片失败：{exc}"], "neighbors": []}

    # ================================================================ 写入

    @server.tool(
        name="dm_store",
        description=(
            "手动写入一条记忆。\n\n"
            "何时用：用户明确要求「记住这条」时。日常对话**不需要**调用——"
            "记忆由摘要流程自动产出。\n"
            "有频率限制，短时间内重复调用会被拒绝。内容与已有切片相同时会返回 duplicate。"
        ),
    )
    @_cold
    def dm_store(
        summary_text: str,
        original_text: str = "",
        title: str | None = None,
        keywords: list[str] | None = None,
        tags: list[str] | None = None,
        db: str | None = None,
        session: str = "default",
    ) -> dict[str, Any]:
        """手动写入记忆切片。

        Args:
            summary_text: 记忆摘要（会被检索的就是它）。
            original_text: 对应原文，可为空。
            title: 简短标题。
            keywords: 关键词，尤其是专有名词与错误码。
            tags: 标签。
            db: 指定库；只允许当前记忆库（前端选定的那个）。
            session: 会话标识，用于限流。
        """
        allowed, remaining = limiter.allow(session)
        if not allowed:
            return {
                "ok": False,
                "error": (
                    f"写入过于频繁（{MANUAL_STORE_WINDOW_SECONDS} 秒内最多 "
                    f"{MANUAL_STORE_LIMIT} 条）。请确认确有必要再写。"
                ),
            }
        active, refusal = _active_or_refuse(db)
        if refusal is not None:
            return refusal
        try:
            payload = svc.store(
                summary_text=summary_text,
                original_text=original_text,
                db=active,
                title=title,
                keywords=keywords,
                tags=tags,
            )
            payload["quota_remaining"] = remaining
            payload["db"] = active
            return payload
        except DuramemError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"写入失败：{exc}"}

    @server.tool(
        name="dm_forget",
        description=(
            "删除一条记忆。\n\n"
            "何时用：用户明确要求忘掉某事时。\n"
            "这是**软删除**——前端可以恢复，所以不必反复确认。"
        ),
    )
    @_cold
    def dm_forget(uid: str, db: str | None = None) -> dict[str, Any]:
        """软删除一条记忆。

        Args:
            uid: 要删除的切片 uid。
            db: 指定库；只允许当前记忆库（前端选定的那个）。
        """
        active, refusal = _active_or_refuse(db)
        if refusal is not None:
            return {**refusal, "uid": uid}
        try:
            return svc.forget(uid, db=active)
        except DuramemError as exc:
            return {"ok": False, "uid": uid, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "uid": uid, "error": f"删除失败：{exc}"}

    # ================================================================ 状态

    @server.tool(
        name="dm_stats",
        description=(
            "查看记忆库概况：当前库、切片数、向量数、消息数、总结进度与异常信号。"
            "用于确认记忆服务是否正常工作。"
        ),
    )
    @_cold
    def dm_stats(db: str | None = None) -> dict[str, Any]:
        """查看库概况（`Service.stats_brief` 的精简投影，与前端状态卡同一份事实）。

        完整统计（文件/WAL 大小、索引后端、逐会话游标、触顶计数…）的消费者
        是**前端管理页**，走 REST `/api/databases/{name}/stats`。曾经这条工具
        原样返回整份——输入只有 `{"db": null}`，输出几百 token 大半是模型既
        用不上也无法行动的字段，纯上下文污染。现在只投影模型关心的：库是谁、
        有多少记忆、检索能不能用、采集活没活着、有没有问题。
        """
        try:
            # 与 dm_search 一样：看状态通常是一轮里最早的调用，顺手刷新本轮的记忆库
            full = svc.stats(db or _turn_db(refresh=True))
            return svc.stats_brief(full, svc.active_view())
        except DuramemError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"获取状态失败：{exc}"}

    return server


def _prepare(db: str | None, data_dir: str | None):
    """构造 Service。--data-dir 必须在读配置前生效。

    stdio 模式下 MCP 子进程的工作目录由宿主决定（通常是用户打开的项目目录），
    不显式指定数据目录就会在别人的项目里生成库文件。
    """
    from pathlib import Path

    from duramem.config import reset_settings

    reset_settings()
    settings = get_settings()
    if data_dir:
        settings.data_dir = Path(data_dir).resolve()
    settings.ensure_dirs()
    if db:
        settings.default_db = db
    service = Service(settings)
    # 刻意**不**在这里解析默认库：指针可能指向一个已被删掉的库，
    # 启动时解析会让整个 MCP 服务器起不来（宿主只会显示"服务器失败"），
    # 而按调用报错能给出"当前记忆库 X 已不存在，请重新选定"这句人话。
    return service


def run_stdio(db: str | None = None, data_dir: str | None = None) -> None:
    build_server(_prepare(db, data_dir)).run(transport="stdio")


def run_http(
    host: str = "127.0.0.1",
    port: int | None = None,
    db: str | None = None,
    data_dir: str | None = None,
) -> None:
    service = _prepare(db, data_dir)
    server = build_server(service)
    import uvicorn

    app = server.streamable_http_app()
    uvicorn.run(app, host=host, port=port or service.settings.backend_port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="duramem-mcp", description="Duramem MCP 服务")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--db",
        default=None,
        help="挂载哪个库。stdio 模式下每个聊天窗口是独立进程，"
        "所以这里就是最干净的库选择方式。",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="数据目录的绝对路径。强烈建议显式指定：子进程的工作目录由宿主决定，"
        "不指定会把库文件散进用户当时打开的项目目录。",
    )
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        run_stdio(args.db, args.data_dir)
    else:
        run_http(args.host, args.port, args.db, args.data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
