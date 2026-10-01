"""FTS5 词法检索。

刻意**不按分数过滤**：先用 `bm25()` 排序，但排序结果只在 RRF 里用排名。
原因是 bm25 分值随语料规模变化剧烈——实测单文档语料下命中会返回 -0.0
（IDF 趋零），照分数做阈值会把正确命中误杀。排名则始终可靠。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from duramem.text.tokenize import Segmenter


@dataclass
class LexicalHit:
    chunk_id: int
    rank: int  # 1-based
    score: float


def search_lexical(
    conn: sqlite3.Connection,
    segmenter: Segmenter,
    query: str,
    k: int = 20,
    tags: Sequence[str] | None = None,
    include_dead: bool = False,
) -> list[LexicalHit]:
    """FTS5 检索。返回按 bm25 升序（越靠前越相关）的候选。"""
    match_expr = segmenter.segment_query(query)
    if not match_expr or k <= 0:
        return []

    clauses = ["f.search_text MATCH ?"]
    params: list[object] = [match_expr]
    if not include_dead:
        clauses.append("c.deleted_at IS NULL")
        clauses.append("c.superseded_by IS NULL")
    for tag in tags or []:
        clauses.append("c.tags LIKE ?")
        params.append(f'%"{tag}"%')

    sql = (
        "SELECT f.rowid AS chunk_id, bm25(chunks_fts) AS score "
        "FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY score LIMIT ?"
    )
    params.append(int(k))

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # 空库或 FTS 表缺失
        return []

    return [
        LexicalHit(chunk_id=int(row["chunk_id"]), rank=index + 1, score=float(row["score"]))
        for index, row in enumerate(rows)
    ]


def lex_hits_to_debug(conn: sqlite3.Connection, hits: Sequence[LexicalHit]) -> list[dict]:
    if not hits:
        return []
    ids = [h.chunk_id for h in hits]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, chunk_uid, title, summary_text, tags FROM chunks WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    by_id = {int(r["id"]): r for r in rows}
    out: list[dict] = []
    for hit in hits:
        row = by_id.get(hit.chunk_id)
        if row is None:
            continue
        out.append(
            {
                "rank": hit.rank,
                "chunk_id": hit.chunk_id,
                "uid": row["chunk_uid"],
                "title": row["title"],
                "summary": row["summary_text"],
                "bm25": round(hit.score, 6),
            }
        )
    return out


__all__ = ["LexicalHit", "lex_hits_to_debug", "search_lexical"]
