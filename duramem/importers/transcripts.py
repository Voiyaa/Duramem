"""JSONL / JSON 形态的转录适配器。

三个源，共同点是"一行一条记录"或"一个数组"：

- **ZCode rollout**：`~/.zcode/cli/rollout/model-io-sess_*.jsonl`。
  每行是一次**模型往返**，`request.messages` 是当时发给模型的**累积**消息数组
  （实测带 `messagesKind: "tail"` 与 `messageOffset`，即可能是被截断的窗口），
  `response.text` 是当轮输出。所以策略是：按行顺序走，用内容哈希去重，
  只收没见过的消息，再补上当轮输出。窗口被截断也不会丢前面的内容。
- **Claude Code**：`~/.claude/projects/<项目>/<uuid>.jsonl`，一行一条消息。
- **通用**：JSON 数组、`{"messages": [...]}`、或 JSONL。字段名容错。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from duramem.importers.base import (
    Conversation,
    flatten_content,
    normalize_role,
    timestamp_to_ms,
    window_from_directory,
)
from duramem.models import Message


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """逐行读 JSONL。坏行跳过而不是整体失败——历史文件里出现个别坏行很常见。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def _peek_lines(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _iter_jsonl(path):
        out.append(item)
        if len(out) >= limit:
            break
    return out


# ====================================================================== ZCode rollout


class ZCodeRolloutImporter:
    name = "zcode-rollout"
    description = "ZCode 模型往返日志（~/.zcode/cli/rollout/*.jsonl）"

    @staticmethod
    def probe(path: Path) -> bool:
        if path.is_dir():
            return any(path.glob("model-io-*.jsonl"))
        if path.suffix.lower() != ".jsonl":
            return False
        return any(item.get("type") == "model_io" for item in _peek_lines(path, 5))

    def _files(self, path: Path, session_filter: list[str] | None) -> list[Path]:
        files = sorted(path.glob("model-io-*.jsonl")) if path.is_dir() else [path]
        if session_filter:
            # 文件名形如 model-io-sess_<id>.jsonl
            wanted = set(session_filter)
            files = [
                f
                for f in files
                if any(key in f.name for key in wanted)
            ]
        return files

    def load(
        self,
        path: Path,
        include_subagents: bool = False,
        session_filter: list[str] | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Conversation]:
        files = self._files(path, session_filter)
        produced = 0
        for file in files:
            entries = [item for item in _iter_jsonl(file) if item.get("type") == "model_io"]
            if not entries:
                continue

            session_id = str(
                entries[0].get("sessionId") or file.stem.replace("model-io-", "")
            )
            # rollout 的会话 id 里带 sess_subagent_ 前缀的通常是子代理
            is_subagent = "subagent" in session_id
            if is_subagent and not include_subagents:
                continue
            if since_ms is not None:
                started = timestamp_to_ms(entries[0].get("startedAt"))
                if started is not None and started < since_ms:
                    continue

            messages: list[Message] = []
            seen: set[tuple[str, str]] = set()
            window: str | None = None

            def add(role: str, content: Any, ts_ms: int | None) -> None:
                nonlocal window
                text = flatten_content(content).strip()
                if not text:
                    return
                key = (role, text)
                if key in seen:
                    return
                seen.add(key)
                messages.append(
                    Message(
                        window_id=window or "zcode:default",
                        session_id=session_id,
                        seq=len(messages),
                        role=role,
                        content=text,
                        ts=None,
                    )
                )

            for entry in entries:
                request = entry.get("request") or {}
                if window is None:
                    window = window_from_directory(
                        request.get("cwd") if isinstance(request.get("cwd"), str) else None,
                        "zcode",
                    )

                for item in request.get("messages") or []:
                    if not isinstance(item, dict):
                        continue
                    role = normalize_role(item.get("role"))
                    if role is None:
                        continue  # tool / system 一律跳过
                    add(role, item.get("content"), None)

                response = entry.get("response") or {}
                add("assistant", response.get("text"), timestamp_to_ms(entry.get("completedAt")))

            if not messages:
                continue

            # window 在 add() 里被延迟展开，这里统一回填
            for message in messages:
                message.window_id = window or "zcode:default"

            produced += 1
            yield Conversation(
                source=self.name,
                origin_id=session_id,
                title=session_id,
                window_id=window or "zcode:default",
                session_id=session_id,
                messages=messages,
                created_at=entries[0].get("startedAt"),
                metadata={"file": file.name, "is_subagent": is_subagent},
            )
            if limit and produced >= limit:
                return


# ====================================================================== Claude Code


class ClaudeCodeImporter:
    name = "claude-code"
    description = "Claude Code 转录（~/.claude/projects/**/*.jsonl）"

    @staticmethod
    def probe(path: Path) -> bool:
        if path.is_dir():
            return any(path.glob("**/*.jsonl"))
        if path.suffix.lower() != ".jsonl":
            return False
        for item in _peek_lines(path, 8):
            message = item.get("message")
            if isinstance(message, dict) and message.get("role"):
                return True
        return False

    def _files(self, path: Path, session_filter: list[str] | None) -> list[Path]:
        if path.is_file():
            return [path]
        files = sorted(path.glob("**/*.jsonl"))
        if session_filter:
            wanted = set(session_filter)
            files = [f for f in files if any(key in f.stem for key in wanted)]
        return files

    def load(
        self,
        path: Path,
        include_subagents: bool = False,
        session_filter: list[str] | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Conversation]:
        produced = 0
        for file in self._files(path, session_filter):
            messages: list[Message] = []
            session_id: str | None = None
            window: str | None = None
            title = file.stem

            for item in _iter_jsonl(file):
                message = item.get("message")
                if not isinstance(message, dict):
                    continue
                role = normalize_role(message.get("role"))
                if role is None:
                    continue
                text = flatten_content(message.get("content")).strip()
                if not text:
                    continue

                session_id = session_id or str(item.get("sessionId") or file.stem)
                window = window or window_from_directory(
                    item.get("cwd") if isinstance(item.get("cwd"), str) else None,
                    "claude",
                )
                if since_ms is not None:
                    ts = timestamp_to_ms(item.get("timestamp"))
                    if ts is not None and ts < since_ms:
                        continue

                messages.append(
                    Message(
                        window_id=window,
                        session_id=session_id,
                        seq=len(messages),
                        role=role,
                        content=text,
                        ts=(
                            item.get("timestamp")
                            if isinstance(item.get("timestamp"), str)
                            else None
                        ),
                        source_msg_id=str(item.get("uuid")) if item.get("uuid") else None,
                    )
                )

            if not messages or not session_id:
                continue
            produced += 1
            yield Conversation(
                source=self.name,
                origin_id=session_id,
                title=title[:80],
                window_id=window or "claude:default",
                session_id=session_id,
                messages=messages,
                created_at=messages[0].ts,
                metadata={"file": str(file)},
            )
            if limit and produced >= limit:
                return


# ====================================================================== 通用


class GenericJsonImporter:
    """通用 JSON / JSONL。字段名容错，用于任何自制的导出文件。"""

    name = "generic"
    description = "通用 JSON / JSONL（含 role 与 content 字段）"

    ROLE_KEYS = ("role", "type", "sender", "speaker", "from")
    CONTENT_KEYS = ("content", "text", "message", "value")
    SESSION_KEYS = ("session_id", "sessionId", "conversation_id", "conversationId", "chat_id")
    WINDOW_KEYS = ("window_id", "windowId", "cwd", "project", "workspace")
    TIME_KEYS = ("ts", "timestamp", "time", "created_at", "createdAt")

    @staticmethod
    def probe(path: Path) -> bool:
        """通用适配器排在最后：它总是能解析点什么，所以只在前面都不匹配时才用。"""
        return path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".ndjson"}

    def _records(self, path: Path) -> list[dict[str, Any]]:
        if path.suffix.lower() in {".jsonl", ".ndjson"}:
            return list(_iter_jsonl(path))
        try:
            loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(loaded, list):
            return [item for item in loaded if isinstance(item, dict)]
        if isinstance(loaded, dict):
            for key in ("messages", "conversation", "data", "items", "records"):
                value = loaded.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            return [loaded]
        return []

    def _pick(self, item: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if item.get(key) not in (None, ""):
                return item[key]
        return None

    def load(
        self,
        path: Path,
        include_subagents: bool = False,
        session_filter: list[str] | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Conversation]:
        records = self._records(path)
        if not records:
            return

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in records:
            role = normalize_role(self._pick(item, self.ROLE_KEYS))
            if role is None:
                continue
            text = flatten_content(self._pick(item, self.CONTENT_KEYS)).strip()
            if not text:
                continue
            if since_ms is not None:
                ts = timestamp_to_ms(self._pick(item, self.TIME_KEYS))
                if ts is not None and ts < since_ms:
                    continue

            session = str(self._pick(item, self.SESSION_KEYS) or path.stem)
            if session_filter and session not in session_filter:
                continue
            raw_window = self._pick(item, self.WINDOW_KEYS)
            window = (
                str(raw_window)
                if raw_window and ("window_id" in item or "windowId" in item)
                else window_from_directory(
                    str(raw_window) if raw_window else None, self.name
                )
            )
            grouped.setdefault((window, session), []).append({"role": role, "content": text})

        produced = 0
        for (window, session), items in grouped.items():
            messages = [
                Message(
                    window_id=window,
                    session_id=session,
                    seq=index,
                    role=item["role"],
                    content=item["content"],
                )
                for index, item in enumerate(items)
            ]
            produced += 1
            yield Conversation(
                source=self.name,
                origin_id=session,
                title=session,
                window_id=window,
                session_id=session,
                messages=messages,
                metadata={"file": str(path)},
            )
            if limit and produced >= limit:
                return


__all__ = ["ClaudeCodeImporter", "GenericJsonImporter", "ZCodeRolloutImporter"]


def default_claude_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".claude" / "projects"


def default_zcode_rollout_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".zcode" / "cli" / "rollout"
