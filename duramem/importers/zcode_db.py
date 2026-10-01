"""ZCode 会话库适配器（首选路径）。

数据源：`~/.zcode/cli/db/db.sqlite`，表结构（实测确认）：

- `session`   —— `id` / `directory` / `title` / `task_type` / `time_created`
- `message`   —— `id` / `session_id` / `sequence` / `data`(JSON，含 `role`) / `time_created`
- `part`      —— `message_id` / `sequence` / `data`(JSON，含 `type` 与 `text`)

关键实测结论：

1. **正文不在 `message` 里，在 `part` 里。** `message.data` 只有 role/时间/模型信息，
   文本落在 `part.data.type == "text"` 的段上。所以必须按 message→parts 拼接。
2. **`part.data.type` 有多种**：`text`（要）、`reasoning`（模型的思考过程，**丢弃**）、
   `tool`（工具调用与结果，丢弃）、`step-start`/`step-finish`/`timeline`/`file`（记账信息，丢弃）。
   把 reasoning 收进记忆会把模型的内心独白当事实，这是明确要避免的。
3. **`session.task_type` 有 `interactive` 与 `subagent_child` 两种**，实测 62 : 439。
   子代理会话是主代理派出去跑的任务，不是用户自己的对话，默认排除——
   否则库里会塞满"Research task (read-only, no code changes)…"这类模板文本。

**全程只读打开源库**（`mode=ro`）。这个文件正被宿主进程使用，任何写入都可能损坏它。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from duramem.importers.base import (
    Conversation,
    flatten_content,
    ms_to_iso,
    normalize_role,
    timestamp_to_ms,
    window_from_directory,
)
from duramem.models import Message

DEFAULT_DB = Path(os.path.expanduser("~")) / ".zcode" / "cli" / "db" / "db.sqlite"

# 只取正文。reasoning 是模型的思考过程，tool 是工具调用与结果——
# 两者进记忆都会把噪声当事实。
KEEP_PART_TYPES = ("text",)

# 助手消息里的 text 段落分两类，靠所在的 step 结束原因区分（实测确认）：
#   step-finish.reason = "tool-calls"  -> 工具调用前的叙述（2795 次）
#   step-finish.reason = "stop"        -> 最终回答（205 次）
# 叙述是模型在跟自己说话，当成记忆检索出来是噪声。默认只收最终回答。
FINAL_STEP_REASONS = ("stop",)

# 非对话内容统一标记成 system 角色。
# **不删掉**：L1 原文要完整，read_original 还得能取到它们；
# 只是让摘要器默认跳过，别把它们当知识切进记忆。
NON_CONVERSATIONAL_ROLE = "system"


def _is_synthetic(meta: dict) -> bool:
    """判断这条 user 消息是不是宿主运行时注入的，而不是用户真的说的话。

    实测标记（任一命中即可）：`synthetic: true`、
    `semantics.origin = "agent_runtime"`、`visibility = "model-only"`、
    `anchor.origin = "synthetic"`。
    实测有 56% 的 user text 段是这类 todo 提醒样板文。
    """
    if meta.get("synthetic") is True:
        return True
    semantics = meta.get("semantics")
    if isinstance(semantics, dict) and semantics.get("origin") == "agent_runtime":
        return True
    if meta.get("visibility") == "model-only":
        return True
    anchor = meta.get("anchor")
    if isinstance(anchor, dict) and anchor.get("origin") == "synthetic":
        return True
    return False


def _synthetic_kind(meta: dict) -> str:
    semantics = meta.get("semantics")
    if isinstance(semantics, dict) and semantics.get("kind"):
        return str(semantics["kind"])
    return str(meta.get("source") or "runtime_injected")


def _assistant_text(chunks: list[dict]) -> tuple[str, str]:
    """按 step 把助手消息的正文分成（最终回答, 过程叙述）。

    规则：按 step 累积 text，靠 `step-finish.reason` 决定这一步归哪边
    （`stop` = 最终回答，`tool-calls` = 工具调用前的叙述）。
    消息里**没有** step 标记时（更早的版本、或结构变了）一律算最终回答——
    只在结构明确告诉我们"这是过程叙述"时才归类。

    两边都返回而不是丢掉叙述：叙述仍要进库（L1 原文的完整性靠它），
    只是标成非对话内容、不参与摘要。
    """
    text_chunks = [c for c in chunks if c.get("type") in KEEP_PART_TYPES]
    if not text_chunks:
        return "", ""

    has_step_markers = any(c.get("type") in {"step-start", "step-finish"} for c in chunks)
    if not has_step_markers:
        return (
            "\n".join(flatten_content(c.get("text")) for c in text_chunks).strip(),
            "",
        )

    finals: list[str] = []
    narrations: list[str] = []
    buffer: list[str] = []
    for chunk in chunks:
        kind = chunk.get("type")
        if kind == "step-start":
            buffer = []
        elif kind in KEEP_PART_TYPES:
            text = flatten_content(chunk.get("text"))
            if text.strip():
                buffer.append(text)
        elif kind == "step-finish":
            reason = chunk.get("reason")
            if reason is None or reason in FINAL_STEP_REASONS:
                finals.extend(buffer)
            else:
                narrations.extend(buffer)
            buffer = []

    # 最后一个 step 没收到 step-finish（流被中断）：保守起见算最终回答
    finals.extend(buffer)
    return "\n".join(finals).strip(), "\n".join(narrations).strip()


def default_db_path() -> Path:
    return DEFAULT_DB


class ZCodeDbImporter:
    name = "zcode-db"
    description = "ZCode 会话库（~/.zcode/cli/db/db.sqlite，只读）"

    @staticmethod
    def probe(path: Path) -> bool:
        if not path.is_file():
            return False
        if path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            return False
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return {"session", "message", "part"} <= names

    # ------------------------------------------------------------------

    def _connect(self, path: Path) -> sqlite3.Connection:
        # mode=ro 是硬要求：这个库正被宿主使用，只读是安全底线
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _sessions(
        self,
        conn: sqlite3.Connection,
        include_subagents: bool,
        session_filter: list[str] | None,
        since_ms: int | None,
        limit: int | None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[object] = []
        if not include_subagents:
            # 没有 task_type 的库（更早的版本）不该被这个条件误杀
            clauses.append("(task_type IS NULL OR task_type <> 'subagent_child')")
        if since_ms is not None:
            clauses.append("time_created >= ?")
            params.append(since_ms)
        if session_filter:
            placeholders = ",".join("?" * len(session_filter))
            clauses.append(f"(id IN ({placeholders}) OR slug IN ({placeholders}))")
            params.extend(session_filter)
            params.extend(session_filter)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM session {where} ORDER BY time_created DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(conn.execute(sql, params))

    def _messages(self, conn: sqlite3.Connection, session_id: str) -> list[dict]:
        """按 message 顺序拼出正文。返回 [{role, content, ts, id, speaker}]。

        两类内容会被标成 `system` 角色（保留但不参与摘要）：
        宿主注入的提醒，以及助手在工具调用之间的叙述。
        """
        rows = conn.execute(
            "SELECT id, sequence, data FROM message WHERE session_id=? ORDER BY sequence",
            (session_id,),
        ).fetchall()
        if not rows:
            return []

        message_ids = [row["id"] for row in rows]
        # 一次取回该会话所有 part，避免逐条消息查一次
        parts: dict[str, list[dict]] = {}
        placeholders = ",".join("?" * len(message_ids))
        for part in conn.execute(
            f"SELECT message_id, sequence, data FROM part "
            f"WHERE message_id IN ({placeholders}) ORDER BY sequence",
            message_ids,
        ):
            try:
                payload = json.loads(part["data"] or "{}")
            except json.JSONDecodeError:
                continue
            parts.setdefault(part["message_id"], []).append(payload)

        out: list[dict] = []
        for row in rows:
            try:
                meta = json.loads(row["data"] or "{}")
            except json.JSONDecodeError:
                continue
            role = normalize_role(meta.get("role"))
            if role is None:
                continue

            chunks = parts.get(row["id"], [])
            speaker: str | None = None

            if role == "user":
                if _is_synthetic(meta):
                    # 宿主注入的提醒不是用户说的话
                    role = NON_CONVERSATIONAL_ROLE
                    speaker = _synthetic_kind(meta)
                content = "\n".join(
                    flatten_content(part.get("text"))
                    for part in chunks
                    if part.get("type") in KEEP_PART_TYPES
                ).strip()
            else:
                final, narration = _assistant_text(chunks)
                if final:
                    content = final
                elif narration:
                    # 只有过程叙述：保留进库（L1 要完整）但标成非对话内容
                    content = narration
                    role = NON_CONVERSATIONAL_ROLE
                    speaker = "assistant_narration"
                else:
                    content = ""

            if not content.strip():
                # 纯工具调用的轮次没有任何文本，跳过而不是塞空消息
                continue

            created = meta.get("time")
            if isinstance(created, dict):
                created = created.get("created")
            out.append(
                {
                    "id": row["id"],
                    "role": role,
                    "content": content.strip(),
                    "ts_ms": timestamp_to_ms(created),
                    "speaker": speaker,
                }
            )
        return out

    # ------------------------------------------------------------------

    def load(
        self,
        path: Path,
        include_subagents: bool = False,
        session_filter: list[str] | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> Iterator[Conversation]:
        conn = self._connect(path)
        try:
            for session_row in self._sessions(
                conn, include_subagents, session_filter, since_ms, limit
            ):
                session_id = session_row["id"]
                raw = self._messages(conn, session_id)
                if not raw:
                    continue

                directory = session_row["directory"]
                window = window_from_directory(directory, "zcode")
                title = (session_row["title"] or "").strip() or session_id

                messages = [
                    Message(
                        window_id=window,
                        # 用源系统的会话 id 原样做 session_id：
                        # 这样 hook 实时采集与历史导入会落到同一会话上，而不是各建一个
                        session_id=session_id,
                        seq=index,
                        role=item["role"],
                        content=item["content"],
                        ts=ms_to_iso(item["ts_ms"]),
                        speaker=item.get("speaker"),
                        source_msg_id=item["id"],
                    )
                    for index, item in enumerate(raw)
                ]

                yield Conversation(
                    source=self.name,
                    origin_id=session_id,
                    title=title[:80],
                    window_id=window,
                    session_id=session_id,
                    messages=messages,
                    created_at=ms_to_iso(timestamp_to_ms(session_row["time_created"])),
                    metadata={
                        "directory": directory,
                        "task_type": session_row["task_type"],
                        "is_subagent": session_row["task_type"] == "subagent_child",
                        "project_id": session_row["project_id"],
                    },
                )
        finally:
            conn.close()


__all__ = ["ZCodeDbImporter", "default_db_path", "DEFAULT_DB"]
