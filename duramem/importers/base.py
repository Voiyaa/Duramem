"""已有对话的导入器。

"把历史对话灌进记忆库"这件事的难点不在写库，而在**读得懂源格式**。
所以这里按源分适配器，每个适配器只回答一个问题：这段历史里有哪些会话、每个会话有哪些消息。

设计取舍：

1. **源库一律只读打开。** ZCode 的 `cli/db/db.sqlite` 正在被宿主使用，
   任何写入都可能损坏它。适配器只读，且不允许调用方改这个行为。
2. **不猜格式。** 自动识别基于内容特征（比如 ZCode rollout 里的 `type=model_io`），
   识别不出来就明确报错并列出支持的形态，而不是拿一个通用解析器硬啃、悄悄导入垃圾。
3. **导入必须幂等。** 反复导入同一份历史不该产生重复记忆——靠
   `(window, session, seq)` 冲突处理加上内容哈希，而不是靠"用户记得别导两次"。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from duramem.models import Message


@dataclass
class Conversation:
    """一个待导入的会话。"""

    source: str
    origin_id: str
    title: str
    window_id: str
    session_id: str
    messages: list[Message] = field(default_factory=list)
    created_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def message_count(self) -> int:
        return len(self.messages)


@dataclass
class ImportReport:
    """导入结果。dry_run 时只有 scanned 部分有意义。"""

    source: str
    dry_run: bool = False
    conversations_found: int = 0
    conversations_imported: int = 0
    messages_imported: int = 0
    skipped_short: int = 0
    skipped_empty: int = 0
    summarized: int = 0
    chunks_added: int = 0
    errors: list[str] = field(default_factory=list)
    preview: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": not self.errors,
            "source": self.source,
            "dry_run": self.dry_run,
            "conversations_found": self.conversations_found,
            "conversations_imported": self.conversations_imported,
            "messages_imported": self.messages_imported,
            "skipped_short": self.skipped_short,
            "skipped_empty": self.skipped_empty,
        }
        if self.summarized:
            payload["summarized"] = self.summarized
            payload["chunks_added"] = self.chunks_added
        if self.errors:
            payload["errors"] = self.errors
        if self.preview:
            payload["preview"] = self.preview
        return payload


class Importer(Protocol):
    name: str
    description: str

    @staticmethod
    def probe(path: Path) -> bool:
        """这个路径是不是本适配器认识的形态。"""
        ...

    def load(
        self,
        path: Path,
        include_subagents: bool = False,
        session_filter: list[str] | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Conversation]:
        ...


@dataclass
class ConversationSummary:
    """只用于"列出来给用户挑"，不含消息正文。"""

    origin_id: str
    title: str
    window_id: str
    session_id: str
    messages: int
    created_at: str | None = None
    is_subagent: bool = False
    first_line: str = ""
    already_imported: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "origin_id": self.origin_id,
            "title": self.title,
            "window_id": self.window_id,
            "session_id": self.session_id,
            "messages": self.messages,
            "created_at": self.created_at,
            "is_subagent": self.is_subagent,
            "first_line": self.first_line,
            "already_imported": self.already_imported,
        }


def summarize_conversation(
    item: Conversation, imported_ids: set[str] | None = None
) -> ConversationSummary:
    first_line = ""
    if item.messages:
        first_line = item.messages[0].content.splitlines()[0][:120]
    return ConversationSummary(
        origin_id=item.origin_id,
        title=item.title,
        window_id=item.window_id,
        session_id=item.session_id,
        messages=item.message_count,
        created_at=item.created_at,
        is_subagent=bool(item.metadata.get("is_subagent")),
        first_line=first_line,
        already_imported=bool(imported_ids and item.session_id in imported_ids),
    )


def survey_source(
    importer: Importer,
    path: Path,
    include_subagents: bool = False,
    session_filter: list[str] | None = None,
    since_ms: int | None = None,
    imported_ids: set[str] | None = None,
) -> list[ConversationSummary]:
    """列出可导入的对话，不写库。

    复用 `load()` 而不是另写一套探查逻辑——两套逻辑迟早会不一致，
    而"列出来的和实际导进去的不是一回事"是最难查的 bug 之一。
    """
    out: list[ConversationSummary] = []
    for item in importer.load(
        path,
        include_subagents=include_subagents,
        session_filter=session_filter,
        since_ms=since_ms,
    ):
        out.append(summarize_conversation(item, imported_ids))
    return out


# ====================================================================== 工具


def normalize_role(role: Any) -> str | None:
    """把各家的 role 词表归一到 user / assistant。

    认不出来就返回 None —— 让调用方丢弃，而不是硬塞一个 role 污染记忆。
    system 也返回 None：系统提示是模板文本，进了记忆只会变成噪声。
    """
    if not isinstance(role, str):
        return None
    lowered = role.strip().lower()
    if lowered in {"user", "human", "我", "用户"}:
        return "user"
    if lowered in {"assistant", "ai", "bot", "model", "助手", "ai助手"}:
        return "assistant"
    return None


def flatten_content(content: Any) -> str:
    """把各种 content 形状拍平成纯文本。

    支持：字符串、`[{"type":"text","text":...}]` 数组、`{"text":...}`。
    刻意**丢弃图片/工具结果**——记忆切片要的是可检索的语义内容，
    把 base64 或工具原始输出塞进去只会污染索引。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "content", "value"):
            if isinstance(content.get(key), str):
                return content[key]
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                # 只收文本段；tool_use / tool_result / image 一律跳过
                if item.get("type") in {"text", "input_text", "output_text"}:
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        parts.append(text)
                elif "text" in item and isinstance(item["text"], str) and "type" not in item:
                    parts.append(item["text"])
        return "\n".join(part for part in parts if part.strip())
    return ""


def window_from_directory(directory: str | None, source: str) -> str:
    """用工作目录给窗口命名：不同项目的记忆应该分开，否则一锅粥。"""
    if not directory:
        return f"{source}:default"
    name = Path(directory.replace("\\", "/").rstrip("/")).name
    return f"{source}:{name}" if name else f"{source}:default"


def timestamp_to_ms(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        # 毫秒与秒都见过，用大小判断
        return int(value if value > 10_000_000_000 else value * 1000)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            if text.isdigit():
                return timestamp_to_ms(int(text))
            from datetime import datetime

            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
        except (ValueError, TypeError):
            return None
    return None


def ms_to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


__all__ = [
    "Conversation",
    "ConversationSummary",
    "Importer",
    "ImportReport",
    "flatten_content",
    "ms_to_iso",
    "normalize_role",
    "summarize_conversation",
    "survey_source",
    "timestamp_to_ms",
    "window_from_directory",
]
