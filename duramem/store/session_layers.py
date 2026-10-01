"""会话层（容器层）的读写：L0 摘要 + L1 概览。

对应 OpenViking 的目录 sidecar。一个 (window_id, session_id) 一份：

- **L1** 是模型产出的概览，四段结构（标题 / 简述 / 覆盖度 / 导航 / 逐切片小节）
- **L0** 由 L1 正文的首段**机械抽取**，不是第二次模型调用——零额外生成成本

切片（条目层）与它的关系是"文件与目录"：切片只有 L0（检索匹配入口）与
L2（原文），L1 是容器级概念。见 `store/schema.py` 的 v2 说明。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from duramem.store.database import Database
from duramem.store.registry import now_iso
from duramem.text.tokenize import Segmenter, estimate_tokens

_WS = re.compile(r"\s+")

# 类里有同名方法 `list`：类体内的 `list[...]` 注解会被 mypy 解析成那个方法
# （valid-type）。注解统一走这个别名，公共方法名保持不变。
LayerRows = list[dict[str, Any]]


def normalize_for_digest(text: str) -> str:
    """L1 正文的规范化：压空白 + 去首尾。**不做大小写折叠。**

    与 `repository.normalize_for_hash` 的差别是刻意的：那个用于去重，折叠大小写
    是对的（"Hello" 和 "hello" 是同一条记忆）；这里用于**变更检测**，折叠会让
    真实的改动被判成"没变"。
    """
    return _WS.sub(" ", (text or "").strip())


def overview_digest(text: str) -> str:
    """L1 正文指纹。只比正文，不比元数据。

    刻意的口径：`coverage_total`、`model_used`、时间戳变了但正文没变，
    说明概览内容其实没变，不该触发重生成。照 OpenViking 的
    `semantic_body_digest`——它同样明确排除 frontmatter 与时间戳。
    """
    return hashlib.sha256(normalize_for_digest(text).encode("utf-8")).hexdigest()


def bump_pending(conn: sqlite3.Connection, window_id: str, session_id: str) -> None:
    """给会话记一次"有待跟上的变化"。

    没有概览就不记——没有东西会陈旧，记了反而会让以后新生成的概览一出生
    就带着一串历史 pending。`UPDATE` 命中不到行时天然是空操作。

    取连接而不自己开事务：它在调用方的事务里跑。
    """
    conn.execute(
        "UPDATE session_layers SET pending_changes = pending_changes + 1 "
        "WHERE window_id = ? AND session_id = ?",
        (window_id, session_id),
    )


@dataclass
class SessionLayerDraft:
    """待写入的会话层。`abstract_text` 通常留空，由 `abstract_from_overview` 派生。"""

    window_id: str
    session_id: str
    overview_text: str
    abstract_text: str = ""
    model_used: str | None = None
    coverage_total: int = 0
    coverage_sampled: int = 0


class SessionLayerStore:
    """会话层的读写。"""

    def __init__(self, db: Database, segmenter: Segmenter) -> None:
        self.db = db
        self.segmenter = segmenter

    # ------------------------------------------------------------------ 读

    def get(self, window_id: str, session_id: str) -> dict[str, Any] | None:
        row = self.db.read_conn.execute(
            "SELECT * FROM session_layers WHERE window_id = ? AND session_id = ?",
            (window_id, session_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_by_session(self, session_id: str) -> dict[str, Any] | None:
        """按会话 id 找。同一 session_id 可能出现在多个 window 下，取最近的。

        为什么允许歧义：模型手里只有检索命中给的 `session_id`，没有 window。
        真出现重名时取更新过的那份，比报错更有用。
        """
        row = self.db.read_conn.execute(
            "SELECT * FROM session_layers WHERE session_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.db.read_conn.execute(
            "SELECT * FROM session_layers ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_slices(self, window_id: str, session_id: str) -> int:
        """该会话当前有多少条有效切片。只数不取，供 freshness 判断用。"""
        row = self.db.read_conn.execute(
            "SELECT count(*) FROM chunks WHERE source_window = ? AND source_session = ? "
            "AND deleted_at IS NULL AND superseded_by IS NULL",
            (window_id, session_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def refresh_candidates(self, limit: int = 0) -> LayerRows:
        """需要看一眼的会话。两类都算候选：

        - 已有概览且 `pending_changes > 0`（子切片变过）
        - 有切片但**从来没有**概览（新会话）

        按"待跟上变化数降序、切片数降序"排：最陈旧、最宽的会话优先，
        这样批量刷新在半途中断时也已经处理掉最该处理的。
        """
        sql = """
        SELECT c.source_window   AS window_id,
               c.source_session  AS session_id,
               count(*)          AS slice_count,
               l.id              AS layer_id,
               l.pending_changes AS pending
        FROM chunks c
        LEFT JOIN session_layers l
               ON l.window_id = c.source_window AND l.session_id = c.source_session
        WHERE c.deleted_at IS NULL AND c.superseded_by IS NULL
          AND c.source_window IS NOT NULL AND c.source_session IS NOT NULL
        GROUP BY c.source_window, c.source_session
        HAVING l.id IS NULL OR l.pending_changes > 0
        ORDER BY coalesce(l.pending_changes, 0) DESC, slice_count DESC
        """
        params: tuple[Any, ...] = ()
        if limit > 0:
            sql += " LIMIT ?"
            params = (int(limit),)
        return [dict(r) for r in self.db.read_conn.execute(sql, params)]

    def count(self) -> int:
        row = self.db.read_conn.execute("SELECT count(*) FROM session_layers").fetchone()
        return int(row[0]) if row else 0

    def slices_of_session(self, window_id: str, session_id: str) -> LayerRows:
        """该会话下参与概览的切片（有效、未取代，按 msg 区间排序）。

        返回**全部**而不是抽样后的结果：调用方需要同时知道"总共几条"与
        "用了哪几条"——覆盖度声明（`coverage_total` / `coverage_sampled`）
        是 L1 里的一段实质内容，两个数都得有。抽样由调用方做。
        """
        sql = (
            "SELECT chunk_uid, title, summary_text, msg_id_start, msg_id_end "
            "FROM chunks WHERE source_window = ? AND source_session = ? "
            "AND deleted_at IS NULL AND superseded_by IS NULL "
            "ORDER BY msg_id_start"
        )
        return [dict(r) for r in self.db.read_conn.execute(sql, (window_id, session_id))]

    def pending(self, window_id: str, session_id: str) -> int:
        row = self.db.read_conn.execute(
            "SELECT pending_changes FROM session_layers "
            "WHERE window_id = ? AND session_id = ?",
            (window_id, session_id),
        ).fetchone()
        return int(row["pending_changes"]) if row is not None else 0

    # ------------------------------------------------------------------ 写

    def upsert(self, draft: SessionLayerDraft) -> dict[str, Any]:
        """写入或更新一份会话层。返回落库后的行。

        `pending_changes` 在成功生成后被清零：这次生成已经消费掉了此前记下的
        所有变化。生成期间新到的变化会继续累加（它们发生在这份概览之后）。
        """
        overview = (draft.overview_text or "").strip()
        if not overview:
            raise ValueError("overview_text 不能为空")
        abstract = (draft.abstract_text or "").strip()
        digest = overview_digest(overview)
        search_text = self.segmenter.build_search_text(abstract, draft.session_id, [])
        ts = now_iso()

        existing = self.get(draft.window_id, draft.session_id)
        if existing is None:
            with self.db.write() as conn:
                cur = conn.execute(
                    "INSERT INTO session_layers("
                    "window_id, session_id, abstract_text, overview_text, search_text,"
                    " abstract_tokens, overview_tokens, model_used, overview_digest,"
                    " pending_changes,"
                    " coverage_total, coverage_sampled, created_at, updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
                    (
                        draft.window_id,
                        draft.session_id,
                        abstract,
                        overview,
                        search_text,
                        estimate_tokens(abstract),
                        estimate_tokens(overview),
                        draft.model_used,
                        digest,
                        int(draft.coverage_total),
                        int(draft.coverage_sampled),
                        ts,
                        ts,
                    ),
                )
            layer_id = int(cur.lastrowid)
        else:
            layer_id = int(existing["id"])
            with self.db.write() as conn:
                conn.execute(
                    "UPDATE session_layers SET abstract_text=?, overview_text=?, search_text=?,"
                    " abstract_tokens=?, overview_tokens=?, model_used=?, overview_digest=?,"
                    " pending_changes=0, coverage_total=?, coverage_sampled=?,"
                    " updated_at=? WHERE id=?",
                    (
                        abstract,
                        overview,
                        search_text,
                        estimate_tokens(abstract),
                        estimate_tokens(overview),
                        draft.model_used,
                        digest,
                        int(draft.coverage_total),
                        int(draft.coverage_sampled),
                        ts,
                        layer_id,
                    ),
                )

        return self.get(draft.window_id, draft.session_id) or {}

    def forget(self, window_id: str, session_id: str) -> bool:
        with self.db.write() as conn:
            cur = conn.execute(
                "DELETE FROM session_layers WHERE window_id = ? AND session_id = ?",
                (window_id, session_id),
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------ 归档导出/还原

    def export_rows(self) -> LayerRows:
        """全量导出会话层行（归档用）。L1 是花模型调用生成的，归档里不能缺。"""
        rows = self.db.read_conn.execute(
            "SELECT * FROM session_layers ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def restore(self, rows: LayerRows) -> int:
        """原样恢复会话层（归档导入用），返回实际写入的份数。

        与 `upsert` 的语义差别：`upsert` 服务于"刚生成了一份新概览"，
        会把 pending_changes 清零——对生成是对的，对还原是错的。
        还原时概览与它引用的切片来自同一份归档，pending 本身就是**要保真的
        数据**（它记录着源库里概览落后了多少）。

        派生字段（search_text / tokens / overview_digest）按当前实现重算：
        分词或摘要口径升级后，派生物应反映新口径，而不是固化旧口径。
        """
        restored = 0
        with self.db.write() as conn:
            for raw in rows:
                row = dict(raw)
                window_id = str(row.get("window_id") or "")
                session_id = str(row.get("session_id") or "")
                overview = (row.get("overview_text") or "").strip()
                if not window_id or not session_id or not overview:
                    continue
                abstract = (row.get("abstract_text") or "").strip()
                cur = conn.execute(
                    "INSERT OR REPLACE INTO session_layers("
                    "window_id, session_id, abstract_text, overview_text, search_text,"
                    " abstract_tokens, overview_tokens, model_used, overview_digest,"
                    " pending_changes,"
                    " coverage_total, coverage_sampled, created_at, updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        window_id,
                        session_id,
                        abstract,
                        overview,
                        self.segmenter.build_search_text(abstract, session_id, []),
                        estimate_tokens(abstract),
                        estimate_tokens(overview),
                        row.get("model_used"),
                        overview_digest(overview),
                        int(row.get("pending_changes") or 0),
                        int(row.get("coverage_total") or 0),
                        int(row.get("coverage_sampled") or 0),
                        row.get("created_at") or now_iso(),
                        row.get("updated_at") or now_iso(),
                    ),
                )
                restored += cur.rowcount
        return restored
