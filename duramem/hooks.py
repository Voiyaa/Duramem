"""ZCode / Claude Code 类宿主的 hook 采集器。

为什么需要它：**MCP 只给模型工具，拿不到原文**。hook 才是采集那一半。
ZCode 支持的事件里，`UserPromptSubmit` 与 `Stop` 分别能拿到用户输入和助手回复，
两者合起来就是完整的对话原文。

一个刻意的设计：**不猜载荷结构，而是容错解析 + 原样落盘。**
宿主文档没有给出 hook 的 stdin JSON 结构（只说了事件名与 matcher 的匹配值），
所以这里同时尝试多种常见字段名，并把收到的原始载荷追加写进
`<data_dir>/hooks/<event>.jsonl`——第一次跑完就能看到真实结构，
而不是靠猜写死一个可能不对的字段名。

输出约束（来自 ZCode hook 规范）：stdout 要么是合法 JSON 要么为空。
任何多余输出都会让 hook 运行被标记为失败，所以本模块只往 stderr 写日志。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duramem.importers.base import window_from_directory
from duramem.models import Message

# 用户输入可能落在这些字段里（不同宿主用词不同）
USER_TEXT_KEYS = ("prompt", "user_prompt", "userPrompt", "message", "text", "content")
# 助手回复可能落在这些字段里
ASSISTANT_TEXT_KEYS = (
    "response",
    "assistant_response",
    "assistantResponse",
    "last_response",
    "lastResponse",
    "preview",
    "message",
    "text",
    "content",
)
SESSION_KEYS = ("session_id", "sessionId", "conversation_id", "conversationId")
TRANSCRIPT_KEYS = ("transcript_path", "transcriptPath")


def _first_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        # 有些宿主把内容包成 {"message": {"content": "..."}}
        if isinstance(value, dict):
            inner = value.get("content") or value.get("text")
            if isinstance(inner, str) and inner.strip():
                return inner
    return None


@dataclass
class HookResult:
    event: str
    messages: list[Message] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_path: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "event": self.event,
            "collected": len(self.messages),
        }
        if self.warnings:
            payload["warnings"] = self.warnings
        if self.raw_path:
            payload["raw_log"] = self.raw_path
        return payload


def log_raw(data_dir: Path, event: str, payload: dict[str, Any]) -> str:
    """把原始载荷追加到 `<data_dir>/hooks/<event>.jsonl`。

    这是这一层最有价值的产出：宿主文档没给载荷结构，先如实记录，
    之后要按真实字段调整解析就有依据，而不是反复试探。
    """
    directory = Path(data_dir) / "hooks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{event or 'unknown'}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return str(path)


def _read_transcript(path: str) -> list[dict[str, Any]]:
    """尽力从 JSONL 转录文件里读出消息。

    只做保守解析：逐行 JSON，取 role/content 形状的条目。
    读不到就返回空——转录格式是宿主私有约定，不该在这里硬猜。
    """
    file = Path(path)
    if not file.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            role = item.get("role") or item.get("type")
            content = item.get("content") or item.get("text") or item.get("message")
            if isinstance(content, list):
                content = " ".join(
                    str(part.get("text", "")) if isinstance(part, dict) else str(part)
                    for part in content
                )
            if isinstance(role, str) and isinstance(content, str) and content.strip():
                out.append({"role": role, "content": content})
    except OSError:
        return []
    return out


def parse_hook_payload(
    payload: dict[str, Any],
    event: str,
    window_id: str | None = None,
    session_id: str | None = None,
    read_transcript: bool = True,
) -> HookResult:
    """把 hook 载荷转成待入库的消息。

    优先用转录文件（能一次补齐整段对话，比逐条拼更完整），
    拿不到再退回载荷里能直接读到的文本。
    """
    result = HookResult(event=event)
    if not isinstance(payload, dict):
        result.warnings.append("载荷不是 JSON 对象，已忽略")
        return result

    session = (
        session_id
        or _first_string(payload, SESSION_KEYS)
        or (f"zcode-{abs(hash(json.dumps(payload, sort_keys=True))) % 10**8}")
    )
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    # 与导入侧共用同一个命名函数：那边是 `zcode:<目录名>`（importers/base.py），
    # 这边早先直接用原始 cwd。同一个会话经两条路进来会被当成两段对话——
    # 唯一键含 window_id，于是"重复投递不产生副本"这条保证就失效了。
    window = window_id or window_from_directory(cwd, "zcode")

    messages: list[dict[str, Any]] = []
    transcript = _first_string(payload, TRANSCRIPT_KEYS)
    if read_transcript and transcript:
        messages = _read_transcript(transcript)
        if not messages:
            result.warnings.append(f"转录文件读不出消息：{transcript}")

    if not messages:
        text = _first_string(
            payload, USER_TEXT_KEYS if event == "UserPromptSubmit" else ASSISTANT_TEXT_KEYS
        )
        if text:
            role = "user" if event == "UserPromptSubmit" else "assistant"
            messages = [{"role": role, "content": text}]
        else:
            result.warnings.append(
                f"载荷里没有可识别的文本，{event} 事件无内容可采集"
            )

    for index, item in enumerate(messages):
        result.messages.append(
            Message(
                window_id=window,
                session_id=session,
                seq=index,
                role=str(item.get("role") or "user"),
                content=str(item["content"]),
            )
        )

    # 用 -1 起算的 seq 会被 upsert 按内容哈希重定位，这里不需要额外处理
    return result


def run_hook(
    event: str,
    data_dir: Path,
    db: str | None = None,
    window_id: str | None = None,
    session_id: str | None = None,
    stdin_stream=None,
    log: bool = True,
) -> dict[str, Any]:
    """hook 的实际入口：读 stdin → 记原始载荷 → 入库。

    任何异常都不向外抛，并且始终返回 ok=True —— **采集失败不该阻塞用户的会话**。
    """
    stream = stdin_stream if stdin_stream is not None else sys.stdin
    warnings: list[str] = []

    try:
        raw = stream.read()
    except Exception as exc:  # noqa: BLE001
        raw = ""
        warnings.append(f"读取 stdin 失败：{exc}")

    payload: dict[str, Any] = {}
    if raw.strip():
        try:
            parsed = json.loads(raw)
            payload = parsed if isinstance(parsed, dict) else {"payload": parsed}
        except json.JSONDecodeError as exc:
            warnings.append(f"stdin 不是合法 JSON：{exc}")
            payload = {"_raw": raw[:4000]}

    raw_path = ""
    if log:
        try:
            raw_path = log_raw(data_dir, event, payload)
        except Exception as exc:  # noqa: BLE001
            # 落盘失败也必须降级：它在下面的 try 之外，一旦抛出整个 hook 就失败了，
            # 而 hook 崩溃是可能影响用户会话的。
            warnings.append(f"原始载荷落盘失败（已忽略）：{exc}")

    try:
        from duramem.config import get_settings
        from duramem.service import Service

        settings = get_settings()
        settings.data_dir = Path(data_dir)
        service = Service(settings)
        try:
            service.ensure_default_db()
            parsed = parse_hook_payload(payload, event, window_id, session_id)
            warnings.extend(parsed.warnings)
            if parsed.messages:
                service.ingest(parsed.messages, db=db, auto_summarize=True)
            return {
                "ok": True,
                "event": event,
                "collected": len(parsed.messages),
                "warnings": warnings,
                "raw_log": raw_path,
            }
        finally:
            service.close()
    except Exception as exc:  # noqa: BLE001
        # 始终 ok=True：hook 失败不该影响用户的会话
        return {
            "ok": True,
            "event": event,
            "collected": 0,
            "warnings": warnings + [f"采集失败（已忽略）：{exc}"],
            "raw_log": raw_path,
        }


__all__ = [
    "ASSISTANT_TEXT_KEYS",
    "HookResult",
    "SESSION_KEYS",
    "TRANSCRIPT_KEYS",
    "USER_TEXT_KEYS",
    "log_raw",
    "parse_hook_payload",
    "run_hook",
]
