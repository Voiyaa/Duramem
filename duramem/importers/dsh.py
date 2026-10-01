"""DeepSeek Harness（dsh）会话导入器。

数据源：`~/.dsh/sessions/<projectKey>/<session-id>/session.v<N>.jsonl[.zstd]`。
格式依据 dsh 仓库 `packages/core/session/src/types.ts`（写方权威）与
`packages/session/session-persistence-jsonl`（落盘实现），本文件写作时点
released format 为 v3（见其 docs/session-format-status.md）。

格式要点：

- 一段会话一个目录；日志文件名把格式版本编进去（`session.v3.jsonl.zstd`）。
  首行是 `{"type": "session", ...}` 头记录，真实 `cwd`、`createdAt` 都在头里。
  目录名是**有损**的路径编码（分隔符折叠 + `~XXXX` 转义），不做反向解码。
- 之后每行一个事件 `{type, seq, time, data, ignorable?}`。与导入相关的：
  `user/message`（`data.source.kind` 区分真人输入与插件注入）、
  `assistant/message`、`tool/call`、`tool/result`、`session/title`。
- zstd 文件是**多帧拼接流**（追加不重压整个文件），必须跨帧解压；压缩是
  可选的（`JsonlCompression = 'zstd' | 'none'`），存在无 `.zstd` 后缀的明文。
- 宿主侧压缩（compaction）不特判：`compaction/summary` 本身不带 surfaceOp，
  真正的范围替换由紧随其后的 `user/message` 事件（`surfaceOp=replace`）执行。
  所以这里通用跟踪 surfaceOp——遇到 replace 就把区间从表面移除。
- 事件词汇表很大且插件可扩展。与对话正文无关的事件一律跳过；未知的
  **非 ignorable** 事件按格式契约意味着"可能来自更新的写入方"，这里不阻塞
  导入（导入器不是会话重建器），但把类型记进 metadata 的
  `unknown_event_types`，不静默。

内容取舍：真人 user 消息 → `role=user`；插件注入（文件变更通知、AGENTS.md、
技能内容等 `source.kind != 'user'`）、工具结果 → `role=system`（进 messages 表
供 L2 window 回查，但不进 L1 渲染）——与 ZCode 适配器对非对话内容的处理一致。
思考流（reasoning 块）不是对话，丢弃。
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from duramem.importers.base import Conversation, ms_to_iso, window_from_directory
from duramem.models import Message

# 事件类型只要求 v3；更高版本意味着更新一代的写入方，词汇与表面语义可能已变，
# 按项目"不猜格式"的原则跳过而不是硬啃。更低版本只会在没有更高代文件时出现
# （迁移是相邻转换），词汇表按 v0-v2 的 dispositions 组织，同样不硬啃。
CURRENT_VERSION = 3

# 表面替换表的值：某条 seq 位置渲染出的 (role, 文本, ts_ms)。
SurfaceCell = tuple[str, str, int | None]
# 展开成一行的消息数据：(seq, role, 文本, ts_ms)——供 load() 逐条转 Message。
SurfaceRow = tuple[int, str, str, int | None]

_FILENAME_RE = re.compile(r"^session\.v(\d+)\.jsonl(\.zstd)?$", re.IGNORECASE)

# 会产生模型可见消息的核心事件；其余核心事件都是日志性标记
_SURFACE_TYPES = {"user/message", "assistant/message", "tool/result"}
_LOG_ONLY_TYPES = {
    "turn/start",
    "turn/end",
    "step/start",
    "step/end",
    "system/message",
    "assistant/attempt",
    "request/header",
    "request/context",
    "session/end-seed",
}


def default_dsh_dir() -> Path:
    return Path.home() / ".dsh" / "sessions"


def _parse_filename(name: str) -> tuple[int, bool] | None:
    """从日志文件名解析（格式版本, 是否 zstd 压缩）。不认识返回 None。"""
    match = _FILENAME_RE.match(name)
    if not match:
        return None
    return int(match.group(1)), bool(match.group(2))


def _find_generation_files(path: Path) -> list[list[Path]]:
    """找出导入目标下的会话日志文件，每个会话目录一组、组内版本从高到低。

    同一会话目录里可能并存多个格式代（迁移留下）：最高版本本工具不认时
    （更新一代的写入方），回落到能认的最高代，而不是整个会话丢掉。
    """
    if path.is_file():
        return [[path]] if _parse_filename(path.name) else []
    if not path.is_dir():
        return []
    candidates = list(path.glob("**/session-*/session.*.jsonl*"))
    if path.name.startswith("session-"):
        candidates.extend(path.glob("session.*.jsonl*"))
    groups: dict[Path, list[tuple[int, Path]]] = {}
    for file in candidates:
        parsed = _parse_filename(file.name)
        if not parsed:
            continue
        groups.setdefault(file.parent, []).append((parsed[0], file))
    return [
        [file for _, file in sorted(items, key=lambda item: item[0], reverse=True)]
        for _, items in sorted(groups.items())
    ]


def _read_lines(path: Path) -> list[str]:
    """读出全部日志行。zstd 按多帧拼接流解压（dsh 追加不重压）。"""
    raw = path.read_bytes()
    if path.suffix.lower() == ".zstd":
        import zstandard

        decompressor = zstandard.ZstdDecompressor()
        with decompressor.stream_reader(io.BytesIO(raw), read_across_frames=True) as reader:
            data = reader.read()
    else:
        data = raw
    return [line for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()]


def _render_blocks(blocks: Any) -> tuple[str, list[str]]:
    """把 ContentBlock[] 渲染成 (文本, 工具调用名列表)。

    text 是正文；图片/文件附件降级为占位符（原文是结构化引用不是文本）；
    reasoning 是思考流不是对话，丢弃；未知块类型留占位符，不静默吞掉。
    """
    texts: list[str] = []
    calls: list[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if text:
                texts.append(str(text))
        elif block_type == "tool-call":
            calls.append(str(block.get("name") or "?"))
        elif block_type == "image":
            texts.append("[图片附件]")
        elif block_type == "file":
            texts.append("[文件附件]")
        elif block_type == "reasoning":
            continue
        elif block_type == "tool-result":
            continue  # 嵌套结果只在 tool/result 事件里展开
        else:
            texts.append(f"[未渲染块 {block_type}]")
    return "\n".join(texts), calls


def _surface_node(
    event_type: str, data: dict[str, Any], tool_names: dict[str, str]
) -> tuple[str, str] | None:
    """把一个表面事件渲染成 (role, 文本)。返回 None 表示无可渲染内容。"""
    message = data.get("message") or {}

    if event_type == "assistant/message":
        text, calls = _render_blocks(message.get("content"))
        if not text and calls:
            text = "[调用工具 " + "、".join(calls) + "]"
        if data.get("interrupted"):
            text = (text + "\n" if text else "") + "[本轮被中断]"
        return ("assistant", text) if text.strip() else None

    if event_type == "user/message":
        text, _ = _render_blocks(message.get("content"))
        if not text.strip():
            return None
        # kind == 'user' 是真人输入；plugin/model/tool 等都是注入的结构性内容
        kind = (message.get("source") or {}).get("kind")
        return ("user" if kind == "user" else "system", text)

    if event_type == "tool/result":
        parts: list[str] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool-result":
                continue
            inner_text, _ = _render_blocks(block.get("content"))
            call_id = block.get("toolCallId")
            # 键统一按 str 存（见 tool/call 写入处），查侧同样转 str——
            # 两边口径一致，非 str 的 id 也能对上。
            name = tool_names.get(str(call_id), str(call_id) if call_id else "?")
            marker = " [错误]" if block.get("isError") else ""
            parts.append(f"[工具结果 {name}{marker}]\n{inner_text}".rstrip())
        return ("system", "\n\n".join(parts)) if parts else None

    return None


def _extract(
    lines: list[str],
) -> tuple[
    dict[str, Any] | None,
    str,
    list[tuple[int, str, str, int | None]],
    list[str],
    int | None,
]:
    """从日志行提取 (header, title, 表面消息, 未知非 ignorable 类型, 最后事件时间)。

    表面消息按表面 seq 排序，每条带 (seq, role, 文本, 事件时间)——seq 用作
    source_msg_id（稳定的源内定位），时间用作消息 ts。
    """
    header: dict[str, Any] | None = None
    try:
        first = json.loads(lines[0])
        if isinstance(first, dict) and first.get("type") == "session":
            header = first
    except json.JSONDecodeError:
        return None, "", [], [], None

    title = ""
    tool_names: dict[str, str] = {}
    surface: dict[int, SurfaceCell] = {}
    unknown: set[str] = set()
    last_time: int | None = None

    for line in lines[1:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        seq = event.get("seq")
        time_ms = event.get("time")
        if isinstance(time_ms, (int, float)):
            last_time = int(time_ms)
        # 显式收窄：event 的值是 Any，不声明目标类型的话下面所有访问都退化成
        # Any | None，类型门禁就形同虚设（mypy 也报不出真问题）。
        raw_data = event.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}

        if event_type == "session/title":
            if data.get("title"):
                title = str(data["title"])
            continue
        if event_type == "tool/call":
            call_id = data.get("callId")
            if call_id:
                tool_names[str(call_id)] = str(data.get("name") or "?")
            continue

        # 表面替换是通用机制（compaction 也走它），不特判压缩事件
        surface_op = event.get("surfaceOp", "append")
        if isinstance(surface_op, dict) and surface_op.get("op") == "replace":
            try:
                start, end = int(surface_op["startSeq"]), int(surface_op["endSeq"])
            except (KeyError, TypeError, ValueError):
                pass
            else:
                for replaced in range(start, end + 1):
                    surface.pop(replaced, None)

        if event_type in _SURFACE_TYPES and isinstance(seq, int):
            node = _surface_node(event_type, data, tool_names)
            if node is not None:
                surface[seq] = (
                    node[0],
                    node[1],
                    int(time_ms) if isinstance(time_ms, (int, float)) else None,
                )
            continue
        if event_type in _LOG_ONLY_TYPES:
            continue
        # 未登记的事件类型：插件扩展或更新版本。ignorable 的按契约跳过；
        # 非 ignorable 的也照跳（导入器不是重建器），但如实记录类型名
        if not event.get("ignorable") and event_type:
            unknown.add(str(event_type))

    ordered = [(seq, *surface[seq]) for seq in sorted(surface)]
    return header, title, ordered, sorted(unknown), last_time


class DshSessionImporter:
    name = "dsh"
    description = "DeepSeek Harness 会话（~/.dsh/sessions，v3 事件日志）"

    @staticmethod
    def probe(path: Path) -> bool:
        if path.is_file():
            return _parse_filename(path.name) is not None
        return bool(_find_generation_files(path))

    def load(
        self,
        path: Path,
        include_subagents: bool = False,
        session_filter: list[str] | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Conversation]:
        wanted = {str(item) for item in session_filter} if session_filter else None
        yielded = 0
        for group in _find_generation_files(path):
            if limit is not None and yielded >= limit:
                return
            header: dict[str, Any] | None = None
            title = ""
            ordered: list[SurfaceRow] = []
            unknown: list[str] = []
            last_time: int | None = None
            generation = ""
            # 组内版本从高到低：坏文件与不认的代（更新的写入方）都向下回落
            for file in group:
                parsed = self._parse_file(file)
                if parsed[0] is None or parsed[0].get("version") != CURRENT_VERSION:
                    continue
                header, title, ordered, unknown, last_time = parsed
                generation = file.name
                break
            if header is None or not ordered:
                continue
            session_id = str(header.get("id") or file.parent.name)
            if wanted is not None and session_id not in wanted:
                continue
            # 无新增事件的会话整段跳过（增量导入的文件层筛子）；
            # 有新增的仍全量吐出——幂等由 (window, session, seq) + 内容哈希兜住
            if wanted is None and since_ms and last_time and last_time <= since_ms:
                continue
            is_subagent = bool(
                header.get("origin") == "subagent" or (header.get("delegationDepth") or 0) > 0
            )
            if is_subagent and not include_subagents:
                continue

            cwd = header.get("cwd")
            window = window_from_directory(cwd if isinstance(cwd, str) else None, "dsh")
            created_ms = header.get("createdAt")

            messages = [
                Message(
                    window_id=window,
                    # 源会话 id 原样用：dsh 侧未来的实时采集与导入落到同一会话
                    session_id=session_id,
                    seq=index,
                    role=role,
                    content=text,
                    ts=ms_to_iso(time_ms) if time_ms else None,
                    source_msg_id=str(seq),
                )
                for index, (seq, role, text, time_ms) in enumerate(ordered)
            ]
            metadata: dict[str, Any] = {
                "cwd": cwd or "",
                "is_subagent": is_subagent,
                "delegation_depth": header.get("delegationDepth") or 0,
                "agent_preset": header.get("agentPreset") or "",
                "generation": generation,
            }
            if unknown:
                metadata["unknown_event_types"] = unknown
            yield Conversation(
                source=self.name,
                origin_id=session_id,
                title=(title.strip() or session_id)[:80],
                window_id=window,
                session_id=session_id,
                messages=messages,
                created_at=(
                    ms_to_iso(int(created_ms)) if isinstance(created_ms, (int, float)) else None
                ),
                metadata=metadata,
            )
            yielded += 1

    @staticmethod
    def _parse_file(
        file: Path,
    ) -> tuple[dict[str, Any] | None, str, list[SurfaceRow], list[str], int | None]:
        """解析一个世代文件 → (头记录, 标题, 有序消息行, 未知事件类型, 最后时间)。

        第三个元素曾经错标成 `list[tuple[str, str]]`（stale 注解），与 `_extract`
        实际返回的四元组行不符——类型门禁上线时抓出来的。
        """
        try:
            lines = _read_lines(file)
        except Exception:  # noqa: BLE001 —— 坏帧/坏文件跳过，别让单文件拖垮整个列表
            return None, "", [], [], None
        if not lines:
            return None, "", [], [], None
        return _extract(lines)
