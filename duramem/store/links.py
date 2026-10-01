"""切片间链接（`links` 表）：迭代检索环路的写入侧地基（设计文档 §16.7）。

读取侧的迭代重建有一条前提：**一条记忆必须有多条到达路径**，否则扩散在
第一跳就断。本模块在写入路径上为切片生成**被动链接**——全部由服务端规则
计算，零模型成本、完全确定性：

- `adjacent`：同会话内按 msg 区间相邻的切片。它是 `dm_read_neighbors` 所用
  邻接关系的检索侧投影，跨会话的多跳靠它走不通（那不是它的职责）。
- `tag` / `keyword`：tags / keywords 交集。**跨会话**生效——这是多跳检索
  真正的到达路径："jieba 的坑"能从 S1 会话走到 S2 会话，靠的就是两边切片
  共享的关键词。

主动链接（`source='active'`，摘要期由模型抽取）是预留的增强位，本表结构
已为它留好字段；它在落地前不存在任何写入方。

维护语义（三条铁律）：

1. 链接是**派生数据**，像向量一样可随时整体重建（`rebuild_all`，等价 CLI
   `duramem links-rebuild`）。
2. 单切片重建（`rebuild_links_for`）写成**双向**行：重建 C 时把涉及 C 的
   被动链接全部删掉再按 C 的当前内容重算两个方向。这样 keywords 编辑后
   只需重建一个切片，图仍然一致。
3. 查询时过滤活性（JOIN chunks 且未删未取代），软删除/恢复因此不需要
   动链接；只有真删除（`hard_delete`）才物理清理。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

# 权重方案。刻意用简单常数而不是可调配置：权重的绝对值只影响扩散排序的
# 相对次序，而三类关系的先后（邻接 > 标签 > 关键词）才是真正要表达的判断。
# 等它们被实测证明设错了，再升级成配置项。
ADJACENT_WEIGHT = 1.0
TAG_WEIGHT_PER = 0.6
TAG_WEIGHT_MAX = 1.2
KEYWORD_WEIGHT_PER = 0.4
KEYWORD_WEIGHT_MAX = 1.2
# 关键词交集的度上限：一个烂大街的词（"bug"）不该让一个切片连上全库。
# 按 (交集数, 对端 id) 取前 N，确定性成立。
KEYWORD_MAX_DEGREE = 20

RELATION_ADJACENT = "adjacent"
RELATION_TAG = "tag"
RELATION_KEYWORD = "keyword"
SOURCE_PASSIVE = "passive"
SOURCE_ACTIVE = "active"


def _load_json_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _alive_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, chunk_uid, keywords, tags, source_window, source_session, msg_id_start"
        " FROM chunks WHERE deleted_at IS NULL AND superseded_by IS NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def _clear_passive_for(conn: sqlite3.Connection, chunk_id: int) -> None:
    conn.execute(
        "DELETE FROM links WHERE source = ? AND (src_id = ? OR dst_id = ?)",
        (SOURCE_PASSIVE, chunk_id, chunk_id),
    )


def _write_pairs(
    conn: sqlite3.Connection,
    chunk_id: int,
    pairs: dict[tuple[int, str], float],
    created_at: str,
) -> int:
    rows = []
    for (other_id, relation), weight in pairs.items():
        rows.append((chunk_id, other_id, relation, SOURCE_PASSIVE, weight, created_at))
        rows.append((other_id, chunk_id, relation, SOURCE_PASSIVE, weight, created_at))
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO links(src_id, dst_id, relation, source, weight, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _compute_pairs(
    me: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[tuple[int, str], float]:
    """按当前内容算一个切片的全部被动链接（不含写库）。

    `me` 与 `rows` 都来自同一份存活切片快照——单条与批量重建共用这套逻辑，
    保证两条入口的结果逐字节一致。
    """
    my_tags = set(_load_json_list(me["tags"]))
    my_keywords = set(_load_json_list(me["keywords"]))
    me_id = int(me["id"])

    pairs: dict[tuple[int, str], float] = {}

    # ---- 同会话邻接：按 msg 区间排序后的前后各一片
    if me["msg_id_start"] is not None and me["source_window"] is not None:
        siblings = sorted(
            (
                r
                for r in rows
                if r["source_window"] == me["source_window"]
                and r["source_session"] == me["source_session"]
                and r["msg_id_start"] is not None
            ),
            key=lambda r: (int(r["msg_id_start"]), int(r["id"])),
        )
        index = next(
            (i for i, r in enumerate(siblings) if int(r["id"]) == me_id), None
        )
        if index is not None:
            for neighbor in (siblings[index - 1] if index > 0 else None,
                             siblings[index + 1] if index + 1 < len(siblings) else None):
                if neighbor is not None:
                    pairs[(int(neighbor["id"]), RELATION_ADJACENT)] = ADJACENT_WEIGHT

    # ---- tags / keywords 交集：跨会话生效，是多跳的真正到达路径
    tag_candidates: list[tuple[int, int]] = []
    keyword_candidates: list[tuple[int, int]] = []
    for row in rows:
        other_id = int(row["id"])
        if other_id == me_id:
            continue
        if (other_id, RELATION_ADJACENT) not in pairs and (other_id, RELATION_TAG) not in pairs:
            shared_tags = len(my_tags & set(_load_json_list(row["tags"])))
            if shared_tags:
                tag_candidates.append((other_id, shared_tags))
        shared_keywords = len(my_keywords & set(_load_json_list(row["keywords"])))
        if shared_keywords:
            keyword_candidates.append((other_id, shared_keywords))

    for other_id, shared in tag_candidates:
        pairs[(other_id, RELATION_TAG)] = min(TAG_WEIGHT_MAX, TAG_WEIGHT_PER * shared)
    keyword_candidates.sort(key=lambda item: (-item[1], item[0]))
    for other_id, shared in keyword_candidates[:KEYWORD_MAX_DEGREE]:
        pairs[(other_id, RELATION_KEYWORD)] = min(
            KEYWORD_WEIGHT_MAX, KEYWORD_WEIGHT_PER * shared
        )
    return pairs


def rebuild_links_for(conn: sqlite3.Connection, chunk_id: int, created_at: str) -> int:
    """重建一个切片的被动链接（双向），返回写下的行数。

    在 `repository.insert_chunk` / `update_chunk` 的写事务里调用——它只做
    SELECT + 局部 DELETE/INSERT，与外层事务同生共死，不存在半写状态。
    批量写入（导入 / 一次总结多条切片）请用 `rebuild_links_for_many`，
    逐条调本函数是 O(N²)：每条都把全库存活切片拉进 Python。
    """
    _clear_passive_for(conn, chunk_id)
    me = conn.execute(
        "SELECT id, chunk_uid, keywords, tags, source_window, source_session, msg_id_start,"
        " deleted_at"
        " FROM chunks WHERE id = ?",
        (chunk_id,),
    ).fetchone()
    if me is None:
        return 0
    me = dict(me)
    if me["deleted_at"] is not None:
        return 0

    rows = _alive_rows(conn)
    by_id = {int(r["id"]): r for r in rows}
    if int(me["id"]) not in by_id:
        return 0

    return _write_pairs(conn, chunk_id, _compute_pairs(me, rows), created_at)


def rebuild_links_for_many(
    conn: sqlite3.Connection, chunk_ids: Sequence[int], created_at: str
) -> int:
    """批量重建多个切片的被动链接（双向），返回写下的行数。

    与逐条调 `rebuild_links_for` 的唯一差别：存活切片表**只读一次**，
    N 条切片共享同一份快照算交集。算法与单条版完全一致——同一事务内
    快照不会变，结果逐字节相同。供 `repository.insert_chunks` 在批量
    写入的事务里收尾调用，"提交即可见时链接已就位"的不变式不破。
    """
    if not chunk_ids:
        return 0
    rows = _alive_rows(conn)
    by_id = {int(r["id"]): r for r in rows}
    written = 0
    for chunk_id in chunk_ids:
        me = by_id.get(int(chunk_id))
        if me is None:
            continue
        written += _write_pairs(conn, int(chunk_id), _compute_pairs(me, rows), created_at)
    return written


def rebuild_all(conn: sqlite3.Connection, created_at: str) -> dict[str, int]:
    """整体重建被动链接（backfill / 归档导入后的补齐）。幂等。

    同一份存活快照算完全部切片——曾经逐条调 `rebuild_links_for`，每条各读
    一次全表，全量 backfill 是 O(N²)。
    """
    conn.execute("DELETE FROM links WHERE source = ?", (SOURCE_PASSIVE,))
    rows = _alive_rows(conn)
    written = 0
    for me in rows:
        written += _write_pairs(conn, int(me["id"]), _compute_pairs(me, rows), created_at)
    return {"chunks": len(rows), "rows": written}


def delete_links_for(conn: sqlite3.Connection, chunk_id: int) -> None:
    """真删除切片时物理清理它涉及的全部链接（含未来的主动链接）。

    软删除不走这里——查询侧按活性过滤，恢复后链接仍然可用。
    """
    conn.execute("DELETE FROM links WHERE src_id = ? OR dst_id = ?", (chunk_id, chunk_id))


def fetch_link_rows(
    conn: sqlite3.Connection, chunk_ids: list[int]
) -> list[dict[str, Any]]:
    """取与给定切片相连的链接行（两个方向都算图的无向边）。

    两端都必须存活：软删除/被取代的切片不参与扩散。已真删除的切片行
    不存在，JOIN 自然丢弃。
    """
    if not chunk_ids:
        return []
    placeholders = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        "SELECT l.src_id, l.dst_id, l.relation, l.weight"
        " FROM links l"
        " JOIN chunks a ON a.id = l.src_id AND a.deleted_at IS NULL AND a.superseded_by IS NULL"
        " JOIN chunks b ON b.id = l.dst_id AND b.deleted_at IS NULL AND b.superseded_by IS NULL"
        f" WHERE l.src_id IN ({placeholders}) OR l.dst_id IN ({placeholders})",
        [*chunk_ids, *chunk_ids],
    ).fetchall()
    return [dict(r) for r in rows]


def count_links(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(source = ?), 0) AS passive"
        " FROM links",
        (SOURCE_PASSIVE,),
    ).fetchone()
    return {"total": int(row["total"]), "passive": int(row["passive"])}
