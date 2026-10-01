"""数据访问层：消息、切片、游标、总结运行记录。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from duramem.models import (
    CONVERSATIONAL_ROLES,
    ChunkDraft,
    Message,
    render_messages_with_offsets,
)
from duramem.store.database import Database
from duramem.store.links import (
    count_links,
    delete_links_for,
    rebuild_links_for,
    rebuild_links_for_many,
)
from duramem.store.links import (
    rebuild_all as rebuild_all_links,
)
from duramem.store.registry import now_iso
from duramem.text.tokenize import Segmenter, estimate_tokens

_UID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_WS = re.compile(r"\s+")


def normalize_for_hash(text: str) -> str:
    """去重用的规范化：压缩空白 + 去首尾 + 大小写折叠。

    目的是让"仅空白或大小写不同"的重复切片刻一为同一内容。
    """
    return _WS.sub(" ", (text or "").strip()).casefold()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()


def new_uid() -> str:
    import secrets

    return "".join(secrets.choice(_UID_ALPHABET) for _ in range(12))


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _json_list(value: Any) -> list[str]:
    """归档字段里的列表：导出侧是 JSON 字符串，手编归档可能是真列表。都收。"""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


class Repository:
    """一个库的全部读写操作。"""

    # 命中计数缓冲的落盘阈值与最长滞留时间（见 bump_hits）。
    HIT_FLUSH_THRESHOLD = 64
    HIT_FLUSH_MAX_AGE_SECONDS = 30.0

    def __init__(self, db: Database, segmenter: Segmenter) -> None:
        self.db = db
        self.segmenter = segmenter
        # 命中计数缓冲：检索是读路径，不该每次都抢全局写锁
        self._hit_lock = threading.Lock()
        self._hit_pending: Counter[str] = Counter()
        self._hit_last_flush = time.monotonic()

    # ================================================================== 消息

    def upsert_messages(self, messages: Sequence[Message]) -> list[int]:
        """幂等写入原始消息，返回对应的本地 id 列表。

        冲突键是 `(window_id, session_id, seq)`：同一位置重复投递不会产生副本；
        内容变了（宿主编辑过消息）则更新，保证 L1 与原文一致。
        """
        if not messages:
            return []

        ids: list[int] = []
        edited: list[int] = []
        with self.db.write() as conn:
            for msg in messages:
                digest = content_hash(msg.content)
                existing = conn.execute(
                    "SELECT id, content_hash FROM messages "
                    "WHERE window_id = ? AND session_id = ? AND seq = ?",
                    (msg.window_id, msg.session_id, msg.seq),
                ).fetchone()

                if existing is None:
                    cur = conn.execute(
                        "INSERT INTO messages("
                        "window_id, session_id, seq, role, speaker, content, ts,"
                        " source_msg_id, content_hash, created_at"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            msg.window_id,
                            msg.session_id,
                            msg.seq,
                            msg.role,
                            msg.speaker,
                            msg.content,
                            msg.ts,
                            msg.source_msg_id,
                            digest,
                            now_iso(),
                        ),
                    )
                    ids.append(int(cur.lastrowid))
                    continue

                msg_id = int(existing["id"])
                if existing["content_hash"] != digest:
                    conn.execute(
                        "UPDATE messages SET role=?, speaker=?, content=?, ts=?,"
                        " source_msg_id=?, content_hash=? WHERE id=?",
                        (
                            msg.role,
                            msg.speaker,
                            msg.content,
                            msg.ts,
                            msg.source_msg_id,
                            digest,
                            msg_id,
                        ),
                    )
                    edited.append(msg_id)
                ids.append(msg_id)

        if edited:
            # 事务提交后再做，让重渲染读到的是新内容
            self.refresh_chunks_for_messages(edited)

        return ids

    def refresh_chunks_for_messages(self, msg_ids: Sequence[int]) -> int:
        """消息内容变了，同步重渲染引用它的切片的 original_text 与字符区间。

        不这么做的话，`original_text` 会是一份过期的原文快照——它不再等于所属消息区间的
        渲染结果，"切片作为原文索引"就名不副实，区间指针也不可校验。
        只更新引用的原文，不改 summary 摘要（摘要是模型当时的判断，不该被静默改写）。

        **这里不碰会话层的待刷新计数**，因为那只在 L0 变时才该动：会话概览 L1 的
        生成输入是各切片的 **L0**，而本方法刻意不改 L0。消息编辑让 L2 过时，
        不让 L1 过时。搞反了会让每次编辑都触发一轮昂贵的概览重生成。
        """
        edited = [int(i) for i in msg_ids]
        if not edited:
            return 0
        lo, hi = min(edited), max(edited)

        affected = self.db.read_conn.execute(
            "SELECT chunk_uid, msg_id_start, msg_id_end FROM chunks "
            "WHERE msg_id_start IS NOT NULL AND msg_id_end IS NOT NULL "
            "AND msg_id_start <= ? AND msg_id_end >= ?",
            (hi, lo),
        ).fetchall()
        if not affected:
            return 0

        updates: list[tuple[str, int, int, str, str]] = []
        for row in affected:
            text, offsets = self.render_range(int(row["msg_id_start"]), int(row["msg_id_end"]))
            if not text or not offsets:
                continue
            updates.append(
                (text, offsets[0][1], offsets[-1][2], now_iso(), row["chunk_uid"])
            )

        if not updates:
            return 0
        with self.db.write() as conn:
            conn.executemany(
                "UPDATE chunks SET original_text=?, original_char_start=?, "
                "original_char_end=?, updated_at=? WHERE chunk_uid=?",
                updates,
            )
        return len(updates)

    def bump_session_pending(
        self, conn: sqlite3.Connection, window_id: str, session_id: str
    ) -> None:
        """给会话记一次"有待跟上的变化"（见 `store/session_layers.bump_pending`）。

        SQL 只有那一处实现，这里只是让调用点读起来顺——本方法在 chunks 的写事务里
        被调用，而会话层与 chunks 同库。
        """
        from duramem.store.session_layers import bump_pending

        bump_pending(conn, window_id, session_id)

    def get_messages(self, msg_ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = [int(i) for i in msg_ids]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self.db.read_conn.execute(
            f"SELECT * FROM messages WHERE id IN ({placeholders}) ORDER BY id", ids
        ).fetchall()
        return [dict(r) for r in rows]

    def get_message_range(
        self, start_id: int, end_id: int, conversational_only: bool = False
    ) -> list[dict[str, Any]]:
        lo, hi = (start_id, end_id) if start_id <= end_id else (end_id, start_id)
        sql = "SELECT * FROM messages WHERE id BETWEEN ? AND ?"
        params: list[Any] = [lo, hi]
        if conversational_only:
            placeholders = ",".join("?" * len(CONVERSATIONAL_ROLES))
            sql += f" AND role IN ({placeholders})"
            params.extend(CONVERSATIONAL_ROLES)
        rows = self.db.read_conn.execute(sql + " ORDER BY id", params).fetchall()
        return [dict(r) for r in rows]

    def expand_range(
        self,
        start_id: int,
        end_id: int,
        before: int,
        after: int,
        conversational_only: bool = True,
        window_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """按 ±N 条消息扩展区间，用于 read_original(mode="window")。

        `before`/`after` 数的是**对话消息**——按条数扩展却把系统提醒也算进去的话，
        "前后各 1 条"可能一条真实对话都扩不到。

        `window_id`/`session_id` 是隔离边界，不能省：`messages.id` 是**全库自增**的，
        不限制会话的话，区间两端会扩到别的对话里去——实测"前后各 1 条"取回的是
        另一个窗口的消息，而返回体里仍标着本切片的 `msg_range`，原文被张冠李戴。
        两者都传才加过滤；不传保持旧行为（没有来源的切片不受影响）。
        """
        role_filter = ""
        role_params: list[Any] = []
        if conversational_only:
            placeholders = ",".join("?" * len(CONVERSATIONAL_ROLES))
            role_filter = f" AND role IN ({placeholders})"
            role_params = list(CONVERSATIONAL_ROLES)

        scope_filter = ""
        scope_params: list[Any] = []
        if window_id is not None and session_id is not None:
            scope_filter = " AND window_id = ? AND session_id = ?"
            scope_params = [window_id, session_id]

        rows = self.db.read_conn.execute(
            f"SELECT * FROM messages WHERE id BETWEEN ? AND ?"
            f"{role_filter}{scope_filter} ORDER BY id",
            (start_id, end_id, *role_params, *scope_params),
        ).fetchall()
        if not rows:
            return []
        all_before = self.db.read_conn.execute(
            f"SELECT * FROM messages WHERE id < ?{role_filter}{scope_filter} "
            "ORDER BY id DESC LIMIT ?",
            (start_id, *role_params, *scope_params, max(0, before)),
        ).fetchall()
        all_after = self.db.read_conn.execute(
            f"SELECT * FROM messages WHERE id > ?{role_filter}{scope_filter} "
            "ORDER BY id ASC LIMIT ?",
            (end_id, *role_params, *scope_params, max(0, after)),
        ).fetchall()
        merged = list(reversed(all_before)) + list(rows) + list(all_after)
        return [dict(r) for r in merged]

    def render_range(
        self,
        start_id: int,
        end_id: int,
        conversational_only: bool = True,
    ) -> tuple[str, list[tuple[int, int, int]]]:
        """按规范化约定渲染区间原文，并给出每条消息的字符偏移。

        默认只渲染对话内容（`role IN (user, assistant)`）。宿主注入的提醒与
        助手的过程叙述仍在库里，但不进 L1 原文——见 `CONVERSATIONAL_ROLES` 的说明：
        混进来会让 L1 无谓膨胀，模型回查时直接被预算拒掉。
        """
        rows = self.get_message_range(start_id, end_id, conversational_only=conversational_only)
        return render_messages_with_offsets(rows)

    def message_span(
        self, start_id: int, end_id: int, conversational_only: bool = True
    ) -> tuple[int, int]:
        """区间在渲染文本中的整体字符范围。"""
        text, offsets = self.render_range(
            start_id, end_id, conversational_only=conversational_only
        )
        if not offsets:
            return (0, 0)
        return (offsets[0][1], offsets[-1][2])

    def max_message_id(self, window_id: str, session_id: str) -> int:
        row = self.db.read_conn.execute(
            "SELECT max(id) AS m FROM messages WHERE window_id=? AND session_id=?",
            (window_id, session_id),
        ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else -1

    def message_count(self, window_id: str | None = None, session_id: str | None = None) -> int:
        if window_id and session_id:
            row = self.db.read_conn.execute(
                "SELECT count(*) AS c FROM messages WHERE window_id=? AND session_id=?",
                (window_id, session_id),
            ).fetchone()
        else:
            row = self.db.read_conn.execute("SELECT count(*) AS c FROM messages").fetchone()
        return int(row["c"])

    def max_message_seq(self, window_id: str, session_id: str) -> int:
        row = self.db.read_conn.execute(
            "SELECT max(seq) AS m FROM messages WHERE window_id=? AND session_id=?",
            (window_id, session_id),
        ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else -1

    def find_message_seq_by_hash(
        self, window_id: str, session_id: str, text: str
    ) -> int | None:
        """按内容哈希反查消息在会话里的位置。

        采集侧用它给消息分配稳定的 seq：聊天客户端会从头裁掉旧消息，
        若按数组下标分配 seq，裁剪后同一位置的内容就变了，去重会互相覆盖。
        """
        row = self.db.read_conn.execute(
            "SELECT seq FROM messages WHERE window_id=? AND session_id=? AND content_hash=? "
            "ORDER BY seq LIMIT 1",
            (window_id, session_id, content_hash(text)),
        ).fetchone()
        return int(row["seq"]) if row else None

    def sessions(self) -> list[dict[str, Any]]:
        rows = self.db.read_conn.execute(
            "SELECT window_id, session_id, count(*) AS messages, max(id) AS last_msg_id "
            "FROM messages GROUP BY window_id, session_id ORDER BY last_msg_id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ================================================================== 切片

    def find_alive_by_hash(self, digest: str) -> dict[str, Any] | None:
        row = self.db.read_conn.execute(
            "SELECT * FROM chunks WHERE content_hash = ? AND deleted_at IS NULL "
            "AND superseded_by IS NULL LIMIT 1",
            (digest,),
        ).fetchone()
        return _row_to_dict(row)

    def build_search_text(self, summary_text: str, title: str, keywords: list[str]) -> str:
        return self.segmenter.build_search_text(summary_text, title, keywords)

    def insert_chunk(
        self,
        draft: ChunkDraft,
        source_db: str | None = None,
        supersede_uid: str | None = None,
        summary_soft_limit: int | None = None,
    ) -> dict[str, Any]:
        """写入一条切片（不含向量）。向量由调用方批量补齐。

        `summary_soft_limit` 提供时用于标记 `summary_truncated`——检索调试面板靠它统计
        "有多少切片触顶"，从而判断 摘要上限设得是否合适。

        批量写入（导入 / 一次总结多条切片）用 `insert_chunks`：逐条走本方法时，
        每条都要把全库存活切片拉进 Python 重建链接，批量是 O(N²)。
        """
        ts = now_iso()
        with self.db.write() as conn:
            result = self._insert_chunk_conn(
                conn, draft, source_db, supersede_uid, summary_soft_limit, ts
            )
            # 被动链接随写入自动维护（设计文档 §16.7）：新切片立刻有到达路径，
            # 不必等 backfill。失败不该让写入失败，但这里在同一事务里，
            # 异常只可能来自 SQL 层本身——那种情况下整笔写入一起回滚才是诚实行为。
            rebuild_links_for(conn, result["id"], ts)
        return result

    def insert_chunks(
        self,
        drafts: Sequence[ChunkDraft],
        source_db: str | None = None,
        summary_soft_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """批量写入切片（不含向量）：单事务入库，链接收尾一次重建。

        逐条 `insert_chunk` 的代价是每条一次全库存活表读取 + Python 交集计算，
        N 条切片 O(N²)。本方法把整批放进**一个事务**，收尾用
        `rebuild_links_for_many` 共享一份快照重建——结果与逐条完全一致，
        且"提交即可见时链接已就位"的不变式不破（链接与切片同生共死，
        崩溃时整批回滚，不存在缺链接的半写状态）。结果顺序与 drafts 一致。
        """
        results: list[dict[str, Any]] = []
        with self.db.write() as conn:
            for draft in drafts:
                results.append(
                    self._insert_chunk_conn(
                        conn, draft, source_db, None, summary_soft_limit, now_iso()
                    )
                )
            if results:
                rebuild_links_for_many(conn, [r["id"] for r in results], now_iso())
        return results

    def _insert_chunk_conn(
        self,
        conn,
        draft: ChunkDraft,
        source_db: str | None,
        supersede_uid: str | None,
        summary_soft_limit: int | None,
        ts: str,
    ) -> dict[str, Any]:
        """在给定写事务里写入一条切片行（不含链接维护）。供两条入口共用。"""
        summary = (draft.summary_text or "").strip()
        if not summary:
            raise ValueError("summary_text 不能为空")

        digest = content_hash(summary)
        search_text = self.build_search_text(summary, draft.title, draft.keywords)
        uid = new_uid()
        tokens = estimate_tokens(summary)
        truncated = 1 if (summary_soft_limit and tokens > summary_soft_limit) else 0

        cur = conn.execute(
            "INSERT INTO chunks("
            "chunk_uid, title, title_suggested, summary_text, search_text, original_text, keywords,"
            " source_db, source_window, source_session, msg_id_start, msg_id_end,"
            " original_char_start, original_char_end, content_hash, weight, tags,"
            " created_at, updated_at,"
            " summary_tokens, summary_truncated"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid,
                draft.title or "",
                draft.title_suggested,
                summary,
                search_text,
                draft.original_text or "",
                json.dumps(draft.keywords, ensure_ascii=False),
                source_db,
                draft.source_window,
                draft.source_session,
                draft.msg_id_start,
                draft.msg_id_end,
                draft.original_char_start,
                draft.original_char_end,
                digest,
                float(draft.weight),
                json.dumps(draft.tags, ensure_ascii=False),
                ts,
                ts,
                tokens,
                truncated,
            ),
        )
        chunk_id = int(cur.lastrowid)

        if supersede_uid:
            conn.execute(
                "UPDATE chunks SET superseded_by=?, updated_at=? WHERE chunk_uid=?",
                (uid, ts, supersede_uid),
            )

        # 新的 L0 是会话概览的新输入，所以该会话多了一处"有待跟上"。
        # 手动写入（dm_store）没有 window/session，天然不记——那种切片
        # 不属于任何会话，也就没有概览会因它陈旧。
        if draft.source_window is not None and draft.source_session is not None:
            self.bump_session_pending(conn, draft.source_window, draft.source_session)

        return {"id": chunk_id, "uid": uid, "content_hash": digest, "summary_tokens": tokens}

    def get_chunk(self, uid: str, include_deleted: bool = False) -> dict[str, Any] | None:
        sql = "SELECT * FROM chunks WHERE chunk_uid = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        return _row_to_dict(self.db.read_conn.execute(sql, (uid,)).fetchone())

    def get_chunk_by_id(self, chunk_id: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self.db.read_conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        )

    def get_chunks_by_ids(self, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        ids = [int(i) for i in ids]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.db.read_conn.execute(
            f"SELECT * FROM chunks WHERE id IN ({placeholders})", ids
        ).fetchall()
        return {int(r["id"]): dict(r) for r in rows}

    def update_chunk(self, uid: str, **fields: Any) -> dict[str, Any]:
        """更新切片。改动文本或关键词时自动重建 search_text。"""
        allowed = {
            "title",
            "title_suggested",
            "summary_text",
            "original_text",
            "keywords",
            "tags",
            "weight",
            "deleted_at",
            "superseded_by",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不支持的字段：{sorted(unknown)}")

        current = self.get_chunk(uid, include_deleted=True)
        if current is None:
            raise KeyError(f"切片不存在：{uid}")

        payload = dict(fields)
        if isinstance(payload.get("keywords"), list):
            payload["keywords"] = json.dumps(payload["keywords"], ensure_ascii=False)
        if isinstance(payload.get("tags"), list):
            payload["tags"] = json.dumps(payload["tags"], ensure_ascii=False)

        needs_resegment = {"summary_text", "title", "keywords"} & set(fields)
        if needs_resegment:
            summary = fields.get("summary_text", current["summary_text"])
            title = fields.get("title", current["title"])
            raw_kw = fields.get("keywords", current["keywords"])
            keywords = json.loads(raw_kw) if isinstance(raw_kw, str) else list(raw_kw or [])
            payload["search_text"] = self.build_search_text(summary, title, keywords)
            payload["summary_tokens"] = estimate_tokens(summary)
        if "summary_text" in fields:
            payload["content_hash"] = content_hash(fields["summary_text"])

        payload["updated_at"] = now_iso()
        columns = ", ".join(f"{k} = ?" for k in payload)
        # L0 是会话 L1 的**生成输入**（概览由各切片的 L0 聚合而成），所以它真的变了
        # 会话概览才陈旧。L2 不影响 L1——概览从不读原文。
        #
        # "真的变了"这个条件不能省：`summary_text` 出现在 fields 里但写回同样内容
        # （界面保存时没改、或只是重存一遍）不该记一次待刷新，否则反复保存就会
        # 把计数推到阈值以上、白白触发一轮模型调用。这对应决策表里的 NOOP 行。
        touches_l1_input = (
            "summary_text" in fields
            and content_hash(str(fields["summary_text"])) != current["content_hash"]
        )
        with self.db.write() as conn:
            conn.execute(
                f"UPDATE chunks SET {columns} WHERE chunk_uid = ?",
                (*payload.values(), uid),
            )
            if touches_l1_input:
                window, session = current["source_window"], current["source_session"]
                if window is not None and session is not None:
                    self.bump_session_pending(conn, window, session)
            # keywords/tags 变了，过它的被动链接就得重算（邻接不受编辑影响，
            # 但交集权重是按当前 tags/keywords 算的）。adjacency 重建一遍也无害。
            if {"keywords", "tags"} & set(fields):
                rebuild_links_for(conn, int(current["id"]), payload["updated_at"])
        return payload

    def soft_delete(self, uid: str) -> bool:
        """软删除。向量保留在 chunks_vec 中，检索侧过滤，恢复时无需重新嵌入。"""
        chunk = self.get_chunk(uid, include_deleted=True)
        if chunk is None or chunk["deleted_at"] is not None:
            return False
        self.update_chunk(uid, deleted_at=now_iso())
        return True

    def restore(self, uid: str) -> bool:
        chunk = self.get_chunk(uid, include_deleted=True)
        if chunk is None or chunk["deleted_at"] is None:
            return False
        self.update_chunk(uid, deleted_at=None)
        return True

    def supersede(self, old_uid: str, new_uid: str) -> None:
        """建立版本链：旧条退出默认召回但保留可查。"""
        self.update_chunk(old_uid, superseded_by=new_uid)

    def hard_delete(self, uid: str) -> dict[str, Any]:
        """**真删除**一条切片：行连同 FTS 索引一起清掉，不可恢复。

        与 `soft_delete` 的分工：软删除是"从召回里拿掉但留着能恢复"，真删除是
        "当作从没记过"。所以它只该由人在界面上点，模型那边只有软删除。

        三件事值得说明：

        - **FTS 索引自动同步**：`chunks_fts` 是 external-content 表，靠触发器跟着
          `chunks` 走，删行即删索引，不需要手工维护。
        - **向量要调用方删**：向量在独立的 `chunks_vec` 里，仓库层不持有索引对象。
          调用方（`Service.purge_chunk`）删完行再删向量；万一向量没删掉也只是留下
          一条孤儿（检索侧按 chunk_id 关联不到行，本来就会被丢掉），不会召回错误内容。
        - **版本链要解引用**：别的切片把它记成 `superseded_by` 时清空，否则会留下
          指向不存在切片的悬空指针。
        """
        chunk = self.get_chunk(uid, include_deleted=True)
        if chunk is None:
            return {"ok": False, "reason": "not_found", "uid": uid}

        chunk_id = int(chunk["id"])
        with self.db.write() as conn:
            unlinked = conn.execute(
                "UPDATE chunks SET superseded_by = NULL WHERE superseded_by = ?", (uid,)
            ).rowcount
            conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
            delete_links_for(conn, chunk_id)
        return {
            "ok": True,
            "uid": uid,
            "chunk_id": chunk_id,
            "title": chunk.get("title") or "",
            "was_deleted": chunk.get("deleted_at") is not None,
            "unlinked_superseded": int(unlinked or 0),
        }

    def bump_hits(self, uids: Sequence[str]) -> None:
        """累加命中计数。只做展示用，不参与排序（避免热门越热门）。

        计数先进内存缓冲，攒够 `HIT_FLUSH_THRESHOLD` 条或滞留超过
        `HIT_FLUSH_MAX_AGE_SECONDS` 秒后，由下一次调用顺手落盘。曾经每次
        检索直写一次：读路径每次都抢全局写锁、强制产生一笔 WAL 写，并发
        检索还会在锁上互相串行。落盘频率降到"每窗口一次"后，写锁只在
        阈值处被碰一下。代价是计数可见性最多滞后一个窗口、进程被硬杀时
        丢窗口内的计数——对纯展示字段是有意的取舍；干净退出前调
        `flush_hits`。
        """
        if not uids:
            return
        flush: Counter[str] | None = None
        with self._hit_lock:
            self._hit_pending.update(uids)
            age = time.monotonic() - self._hit_last_flush
            if (
                len(self._hit_pending) >= self.HIT_FLUSH_THRESHOLD
                or age >= self.HIT_FLUSH_MAX_AGE_SECONDS
            ):
                flush = self._hit_pending
                self._hit_pending = Counter()
                self._hit_last_flush = time.monotonic()
        if flush:
            self._flush_hits(flush)

    def flush_hits(self) -> int:
        """立刻把缓冲中的命中计数落盘，返回落盘的计数总次数。

        供干净退出与"导出/统计前要拿到最新计数"的路径显式调用。
        """
        with self._hit_lock:
            pending: Counter[str] = self._hit_pending
            self._hit_pending = Counter()
            if pending:
                self._hit_last_flush = time.monotonic()
        total = sum(pending.values())
        if total:
            self._flush_hits(pending)
        return total

    def _flush_hits(self, counts: Counter[str]) -> None:
        # 缓冲合并了多次检索，同一切片可能反复出现，必须按出现次数加权；
        # 单次检索内部 uid 唯一（hits 按 id 去重），行为与旧逐次 +1 一致。
        with self.db.write() as conn:
            conn.executemany(
                "UPDATE chunks SET hit_count = hit_count + ? WHERE chunk_uid = ?",
                [(count, uid) for uid, count in counts.items()],
            )

    def _chunk_filters(
        self,
        query: str | None,
        tags: Sequence[str] | None,
        include_deleted: bool,
        include_superseded: bool,
    ) -> tuple[str, list[Any]]:
        """列表与计数必须用**同一套**过滤条件。

        否则前端会显示"共 11 条…显示前 10"这种自相矛盾的信息，
        看起来像结果被截断了，其实只是两个数字的统计口径不同。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if not include_superseded:
            clauses.append("superseded_by IS NULL")
        if query:
            clauses.append("(summary_text LIKE ? OR title LIKE ? OR keywords LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        for tag in tags or []:
            clauses.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_chunks(
        self,
        query: str | None = None,
        tags: Sequence[str] | None = None,
        include_deleted: bool = False,
        include_superseded: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = self._chunk_filters(query, tags, include_deleted, include_superseded)
        rows = self.db.read_conn.execute(
            f"SELECT * FROM chunks {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_chunks_matching(
        self,
        query: str | None = None,
        tags: Sequence[str] | None = None,
        include_deleted: bool = False,
        include_superseded: bool = False,
    ) -> int:
        where, params = self._chunk_filters(query, tags, include_deleted, include_superseded)
        row = self.db.read_conn.execute(
            f"SELECT count(*) AS c FROM chunks {where}", params
        ).fetchone()
        return int(row["c"])

    def count_chunks(self, alive_only: bool = True) -> int:
        sql = "SELECT count(*) AS c FROM chunks"
        if alive_only:
            sql += " WHERE deleted_at IS NULL AND superseded_by IS NULL"
        return int(self.db.read_conn.execute(sql).fetchone()["c"])

    def alive_ids(self) -> list[int]:
        rows = self.db.read_conn.execute(
            "SELECT id FROM chunks WHERE deleted_at IS NULL AND superseded_by IS NULL ORDER BY id"
        ).fetchall()
        return [int(r["id"]) for r in rows]

    def chunks_without_vectors(self) -> list[dict[str, Any]]:
        rows = self.db.read_conn.execute(
            "SELECT c.* FROM chunks c LEFT JOIN chunks_vec v ON v.chunk_id = c.id "
            "WHERE v.chunk_id IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]

    def neighbours(
        self, chunk: dict[str, Any], before: int = 1, after: int = 1
    ) -> list[dict[str, Any]]:
        """取相邻切片（含当前切片本身，输出里用 current 标记）。

        按所属会话内的消息区间排序。必须把当前切片也取回来才能定位它在序列中的
        位置——先排除它再去找它的索引会让结果永远为空。
        """
        if chunk.get("msg_id_start") is None or chunk.get("msg_id_end") is None:
            return []
        rows = self.db.read_conn.execute(
            "SELECT * FROM chunks WHERE source_window IS ? AND source_session IS ? "
            "AND deleted_at IS NULL AND superseded_by IS NULL "
            "ORDER BY msg_id_start",
            (chunk.get("source_window"), chunk.get("source_session")),
        ).fetchall()
        items = [dict(r) for r in rows]
        index = next(
            (i for i, item in enumerate(items) if item["chunk_uid"] == chunk["chunk_uid"]),
            None,
        )
        if index is None:
            return []
        return items[max(0, index - before) : index + after + 1]

    # ================================================================== 游标

    def get_cursor(self, window_id: str, session_id: str) -> int:
        row = self.db.read_conn.execute(
            "SELECT last_summarized_msg_id AS v FROM summary_cursors "
            "WHERE window_id=? AND session_id=?",
            (window_id, session_id),
        ).fetchone()
        return int(row["v"]) if row else -1

    def set_cursor(self, window_id: str, session_id: str, msg_id: int) -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO summary_cursors(window_id, session_id,"
                " last_summarized_msg_id, updated_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(window_id, session_id) DO UPDATE SET"
                " last_summarized_msg_id=excluded.last_summarized_msg_id,"
                " updated_at=excluded.updated_at",
                (window_id, session_id, int(msg_id), now_iso()),
            )

    def cursors(self) -> list[dict[str, Any]]:
        rows = self.db.read_conn.execute(
            "SELECT * FROM summary_cursors ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ================================================================== 总结记录

    def record_summary_run(
        self,
        window_id: str | None,
        session_id: str | None,
        msg_id_start: int | None,
        msg_id_end: int | None,
        chunks_added: int,
        duplicates: int,
        rejected: int,
        warnings: Sequence[str],
        model_used: str,
        duration_ms: int,
    ) -> int:
        with self.db.write() as conn:
            cur = conn.execute(
                "INSERT INTO summary_runs("
                "window_id, session_id, msg_id_start, msg_id_end, chunks_added,"
                " duplicates, rejected, warnings, model_used, duration_ms, created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    window_id,
                    session_id,
                    msg_id_start,
                    msg_id_end,
                    chunks_added,
                    duplicates,
                    rejected,
                    json.dumps(list(warnings), ensure_ascii=False),
                    model_used,
                    duration_ms,
                    now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def summary_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.read_conn.execute(
            "SELECT * FROM summary_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ================================================================== 归档还原

    def all_messages(self) -> list[dict[str, Any]]:
        rows = self.db.read_conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def restore_messages(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """原样恢复消息行（归档导入用），返回实际写入的条数。

        **id 必须逐字保留**：`chunks.msg_id_start/end` 与
        `summary_cursors.last_summarized_msg_id` 指向的就是这个值，
        重新编号会让全部区间指针失效——这是往返的硬约束，不是优化。
        目标库是新建的空库，不存在主键冲突；归档内部的重复行用
        INSERT OR IGNORE 跳过并体现在返回值里。
        """
        restored = 0
        with self.db.write() as conn:
            for raw in rows:
                row = dict(raw)
                if row.get("id") is None:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO messages("
                    "id, window_id, session_id, seq, role, speaker, content, ts,"
                    " source_msg_id, content_hash, created_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        int(row["id"]),
                        row["window_id"],
                        row["session_id"],
                        int(row["seq"]),
                        row["role"],
                        row.get("speaker"),
                        row["content"],
                        row.get("ts"),
                        row.get("source_msg_id"),
                        row.get("content_hash") or content_hash(row["content"]),
                        row.get("created_at") or now_iso(),
                    ),
                )
                restored += cur.rowcount
        return restored

    def restore_cursors(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """原样恢复总结游标（含 updated_at——它记录的是源库的总结时间）。"""
        restored = 0
        with self.db.write() as conn:
            for raw in rows:
                row = dict(raw)
                if not row.get("window_id") or not row.get("session_id"):
                    continue
                cur = conn.execute(
                    "INSERT OR REPLACE INTO summary_cursors("
                    "window_id, session_id, last_summarized_msg_id, updated_at"
                    ") VALUES (?,?,?,?)",
                    (
                        row["window_id"],
                        row["session_id"],
                        int(row.get("last_summarized_msg_id") or -1),
                        row.get("updated_at") or now_iso(),
                    ),
                )
                restored += cur.rowcount
        return restored

    def restore_runs(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """恢复总结运行记录。id 重新编号（自增即可），其余字段保留。"""
        restored = 0
        with self.db.write() as conn:
            for raw in rows:
                row = dict(raw)
                cur = conn.execute(
                    "INSERT INTO summary_runs("
                    "window_id, session_id, msg_id_start, msg_id_end, chunks_added,"
                    " duplicates, rejected, warnings, model_used, duration_ms, created_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row.get("window_id"),
                        row.get("session_id"),
                        row.get("msg_id_start"),
                        row.get("msg_id_end"),
                        int(row.get("chunks_added") or 0),
                        int(row.get("duplicates") or 0),
                        int(row.get("rejected") or 0),
                        row.get("warnings") or "[]",
                        row.get("model_used"),
                        row.get("duration_ms"),
                        row.get("created_at") or now_iso(),
                    ),
                )
                restored += cur.rowcount
        return restored

    def restore_chunks(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> dict[str, int]:
        """原样恢复切片行（归档导入用），返回计数。

        与 `insert_chunk` 的差别是刻意的：那条路径为"新记忆"设计——生成新 uid、
        新时间戳、按当前上限标记触顶、给会话概览记 pending。对还原来说这些全是
        错的：还原的切片已经有身份；`deleted_at` / `superseded_by` / `hit_count` /
        `summary_truncated` 是要保真的数据；概览的状态随归档一起还原，本来就是一致的，
        不该被记成"有待跟上"。

        只重算两个派生字段：`content_hash`（去重不变式的根基，必须由当前实现
        保证）与 `search_text`（分词口径可能已升级，派生物应反映新口径）。
        """
        restored = 0
        skipped_empty = 0
        duplicates = 0
        seen: set[str] = set()
        with self.db.write() as conn:
            for raw in rows:
                row = dict(raw)
                summary = (row.get("summary_text") or "").strip()
                if not summary:
                    skipped_empty += 1
                    continue
                uid = str(row.get("chunk_uid") or "").strip()
                if not uid or uid in seen:
                    duplicates += 1
                    continue
                seen.add(uid)

                keywords = _json_list(row.get("keywords"))
                tags = _json_list(row.get("tags"))
                title = str(row.get("title") or "")
                summary_tokens = int(row.get("summary_tokens") or estimate_tokens(summary))
                truncated = int(1 if row.get("summary_truncated") else 0)

                cur = conn.execute(
                    "INSERT OR IGNORE INTO chunks("
                    "chunk_uid, title, title_suggested, summary_text, search_text, original_text,"
                    " keywords, source_db, source_window, source_session, msg_id_start,"
                    " msg_id_end, original_char_start, original_char_end, content_hash, hit_count,"
                    " weight, tags, created_at, updated_at, deleted_at, superseded_by,"
                    " summary_tokens, summary_truncated"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        uid,
                        title,
                        row.get("title_suggested"),
                        summary,
                        self.build_search_text(summary, title, keywords),
                        row.get("original_text") or "",
                        json.dumps(keywords, ensure_ascii=False),
                        row.get("source_db"),
                        row.get("source_window"),
                        row.get("source_session"),
                        row.get("msg_id_start"),
                        row.get("msg_id_end"),
                        row.get("original_char_start"),
                        row.get("original_char_end"),
                        content_hash(summary),
                        int(row.get("hit_count") or 0),
                        float(row.get("weight") or 1.0),
                        json.dumps(tags, ensure_ascii=False),
                        row.get("created_at") or now_iso(),
                        row.get("updated_at") or now_iso(),
                        row.get("deleted_at"),
                        row.get("superseded_by"),
                        summary_tokens,
                        truncated,
                    ),
                )
                restored += cur.rowcount
            if restored:
                # 归档导入是批量写路径，逐条重建浪费；收尾一次整体重建。
                # 链接是派生数据，这样做的结果与逐条重建完全一致。
                rebuild_all_links(conn, now_iso())
        return {
            "restored": restored,
            "skipped_empty": skipped_empty,
            "duplicates": duplicates,
        }

    # ================================================================== 统计

    def rebuild_links(self) -> dict[str, int]:
        """整体重建被动链接（backfill 入口：CLI `links-rebuild` / REST /api/links/rebuild）。"""
        with self.db.write() as conn:
            return rebuild_all_links(conn, now_iso())

    def link_counts(self) -> dict[str, int]:
        return count_links(self.db.read_conn)

    def stats(self) -> dict[str, Any]:
        t = self.db.read_conn.execute("SELECT count(*) AS c FROM chunks").fetchone()["c"]
        alive = self.count_chunks(alive_only=True)
        deleted = self.db.read_conn.execute(
            "SELECT count(*) AS c FROM chunks WHERE deleted_at IS NOT NULL"
        ).fetchone()["c"]
        superseded = self.db.read_conn.execute(
            "SELECT count(*) AS c FROM chunks WHERE superseded_by IS NOT NULL"
        ).fetchone()["c"]
        truncated = self.db.read_conn.execute(
            "SELECT count(*) AS c FROM chunks WHERE summary_truncated = 1"
        ).fetchone()["c"]
        vector_rows = self.db.read_conn.execute(
            "SELECT count(*) AS c FROM chunks_vec"
        ).fetchone()["c"]
        # 真正该关心的是"有多少切片缺向量"——软删除/被取代的切片按设计保留向量
        # （为了能恢复），所以拿"向量数"和"有效切片数"相比会得出错误的结论。
        missing_vectors = self.db.read_conn.execute(
            "SELECT count(*) AS c FROM chunks c LEFT JOIN chunks_vec v ON v.chunk_id = c.id "
            "WHERE v.chunk_id IS NULL"
        ).fetchone()["c"]
        return {
            "chunks_total": int(t),
            "chunks_alive": int(alive),
            "chunks_deleted": int(deleted),
            "chunks_superseded": int(superseded),
            "chunks_missing_vectors": int(missing_vectors),
            "summary_truncated": int(truncated),
            "messages": self.message_count(),
            "vectors": int(vector_rows),
            "links": count_links(self.db.read_conn)["total"],
            "size_bytes": self.db.size_bytes,
            "wal_bytes": self.db.wal_bytes,
            "vector_source": self.db.vector_source,
            "vectors_stale": self.db.vectors_stale,
            "meta": self.db.meta,
        }
