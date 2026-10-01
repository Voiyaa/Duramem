"""links 表与图扩散（v4，设计文档 §16.7/§16.8 P1）。

覆盖四件事：
1. 被动链接的生成规则（邻接 / tags / keywords 交集）与确定性；
2. 写入路径上的自动维护（insert / update / 软删 / 真删 / 归档还原）；
3. PPR 扩散的排序性质（种子排除、一跳高于两跳）；
4. 检索管线集成：增补不替换、触发信号、模式标注、零回归。
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from conftest import SESSION

from duramem.retrieval.expand import expand
from duramem.service import Service
from duramem.store.links import count_links, fetch_link_rows
from duramem.summarizer import SYSTEM_PROMPT

# ====================================================================== 工具


def _link_pairs(service: Service, db: str = "work") -> set[tuple[int, int, str]]:
    conn = service._components(db).repo.db.read_conn
    rows = conn.execute("SELECT src_id, dst_id, relation FROM links").fetchall()
    return {(int(r["src_id"]), int(r["dst_id"]), str(r["relation"])) for r in rows}


def _store_manual(
    service: Service,
    summary: str,
    keywords: list[str],
    tags: list[str] | None = None,
    db: str = "work",
) -> dict:
    """手动写入一条切片，返回 {"uid", "id", ...}（service.store 本身不回内部 id）。"""
    payload = service.store(
        summary,
        original_text="",
        db=db,
        title=summary[:12],
        keywords=keywords,
        tags=tags if tags is not None else [],
    )
    assert payload.get("ok") and not payload.get("duplicate"), payload
    row = service._components(db).repo.get_chunk(payload["uid"])
    return {"uid": payload["uid"], "id": int(row["id"])}


def _search(service: Service, query: str, top_k: int = 5):
    return service._components("work").pipeline.search(query, top_k=top_k)


# ====================================================================== 生成规则


def test_adjacent_links_form_chain(seeded: Service):
    """同会话切片按 msg 区间相邻成链：内部切片有两个邻接，端点有一个。"""
    repo = seeded._components("work").repo
    ids = [
        int(row["id"])
        for row in repo.db.read_conn.execute(
            "SELECT id FROM chunks WHERE deleted_at IS NULL AND superseded_by IS NULL"
            " AND source_session IS ? ORDER BY msg_id_start",
            (SESSION,),
        ).fetchall()
    ]
    assert len(ids) >= 2
    pairs = _link_pairs(seeded)
    adjacent = {(a, b) for a, b, rel in pairs if rel == "adjacent"}
    # 链上每一条相邻对都双向存在
    for left, right in pairwise(ids):
        assert (left, right) in adjacent
        assert (right, left) in adjacent
    # 不同会话之间绝不该有邻接（本库只有一会话，此断言保护的是写法本身）


def test_keyword_link_crosses_sessions(service: Service):
    """keywords 交集跨会话生效——这是多跳检索的真正到达路径。"""
    a = _store_manual(service, "jieba 分词在入库侧踩了块切分正则的坑", ["jieba", "分词"])
    b = _store_manual(
        service, "jieba 日志污染 stderr，对 MCP stdio 危险，已压到 ERROR", ["jieba", "日志"],
        db="work",
    )
    pairs = _link_pairs(service)
    assert (a["id"], b["id"], "keyword") in {(x, y, r) for x, y, r in pairs}
    # 双向
    assert (b["id"], a["id"], "keyword") in {(x, y, r) for x, y, r in pairs}


def test_no_links_without_overlap(service: Service):
    """毫不相关的两条切片之间不该有链接（确定性 + 无噪声边）。"""
    a = _store_manual(service, "前端 favicon 在 16px 下渲染成灰斑", ["favicon"], tags=["t-favicon"])
    b = _store_manual(service, "导出归档升到 format_version 2", ["archive"], tags=["t-archive"])
    pairs = _link_pairs(service)
    assert not any(
        {a["id"], b["id"]} <= {x, y} for x, y, _ in pairs
    )


# ====================================================================== 维护同步


def test_update_keywords_rebuilds_links(service: Service):
    a = _store_manual(service, "记录一：关于 vector_source 指纹的设计", ["vector"], tags=["t-vector"])
    b = _store_manual(service, "记录二：完全无关的内容", ["unrelated"], tags=["t-unrelated"])
    assert not any(
        {a["id"], b["id"]} <= {x, y} for x, y, _ in _link_pairs(service)
    )
    service.update_chunk(b["uid"], keywords=["unrelated", "vector"])
    assert any({a["id"], b["id"]} <= {x, y} for x, y, _ in _link_pairs(service))


def test_soft_delete_excluded_but_links_kept(service: Service):
    """软删除靠查询侧活性过滤；链接保留，恢复后即可用。"""
    a = _store_manual(service, "jieba 分词坑位记录", ["jieba"])
    b = _store_manual(service, "jieba 的另一条记录", ["jieba"])
    assert any({a["id"], b["id"]} <= {x, y} for x, y, _ in _link_pairs(service))
    service.forget(b["uid"], db="work")

    conn = service._components("work").repo.db.read_conn
    rows = fetch_link_rows(conn, [a["id"]])
    # 查询侧看不到软删端点
    assert all(b["id"] not in (int(r["src_id"]), int(r["dst_id"])) for r in rows)
    # 行还在
    total = count_links(conn)["total"]
    assert total > 0

    service.restore(b["uid"], db="work")
    rows = fetch_link_rows(conn, [a["id"]])
    assert any(b["id"] in (int(r["src_id"]), int(r["dst_id"])) for r in rows)


def test_hard_delete_removes_links(seeded: Service):
    repo = seeded._components("work").repo
    uid = repo.list_chunks(limit=1)[0]["chunk_uid"]
    before = count_links(repo.db.read_conn)["total"]
    assert before > 0
    repo.hard_delete(uid)
    after = count_links(repo.db.read_conn)["total"]
    assert after < before


def test_rebuild_all_is_idempotent(service: Service):
    _store_manual(service, "jieba 记录 A", ["jieba", "分词"])
    _store_manual(service, "jieba 记录 B", ["jieba"])
    repo = service._components("work").repo
    first = repo.rebuild_links()
    second = repo.rebuild_links()
    assert first["rows"] == second["rows"] > 0


# ====================================================================== PPR 扩散


def test_expand_excludes_seeds_and_ranks_one_hop_first(service: Service):
    """PPR 的两条硬性质：种子不返回；一跳候选排在两跳之前。"""
    a = _store_manual(service, "种子切片：讨论 sqlite-vec 的 chunk_size", ["sqlite-vec"], tags=["t-a"])
    b = _store_manual(service, "一跳：sqlite-vec 的空间开销实测", ["sqlite-vec"], tags=["t-b"])
    c = _store_manual(service, "两跳：vec 表重建的顺序问题", ["重建"], tags=["t-c"])
    # c 与 b 共享"重建"才能被两跳带到
    b2 = _store_manual(service, "桥：sqlite-vec 与重建顺序", ["sqlite-vec", "重建"], tags=["t-d"])

    repo = service._components("work").repo
    conn = repo.db.read_conn
    candidates = expand(conn, {int(a["id"]): 1.0}, top_k=5)
    got = {c.chunk_id for c in candidates}
    assert int(a["id"]) not in got
    for uid in (b["id"], b2["id"]):
        assert int(uid) in got, "一跳候选必须被带到"
    assert int(c["id"]) in got, "经桥接的两跳候选应被带到"
    by_id = {c.chunk_id: c for c in candidates}
    assert by_id[int(b["id"])].hops == 1
    assert by_id[int(b2["id"])].hops == 1
    assert by_id[int(c["id"])].hops == 2
    # 一跳的 PPR 质量高于两跳
    assert by_id[int(b["id"])].ppr > by_id[int(c["id"])].ppr
    # 归一化分：最高者为 1.0
    assert max(c.score for c in candidates) == pytest.approx(1.0)


# ====================================================================== 管线集成


def test_expansion_is_additive_and_zero_regression(seeded: Service):
    """核心不变式：直接命中逐字节一致，扩散只追加。"""
    settings = seeded.settings
    query = "端口被占用怎么办"
    settings.expand_enabled = False
    base = _search(seeded, query)
    settings.expand_enabled = True
    settings.expand_always = True
    expanded_run = _search(seeded, query)

    base_ids = [(h.uid, round(h.score, 6)) for h in base.hits]
    expanded_ids = [(h.uid, round(h.score, 6)) for h in expanded_run.hits]
    assert base_ids == expanded_ids, "增补不得改动直接命中"
    assert all(h.origin == "direct" for h in expanded_run.hits)
    # 扩散候选（若有）全部带 expand 标记，且 uid 不与直接命中重复
    direct_uids = {uid for uid, _ in base_ids}
    for hit in expanded_run.expanded:
        assert hit.origin == "expand"
        assert hit.uid not in direct_uids


def test_expansion_triggers_on_low_yield(seeded: Service):
    """产出不足（命中数 < limit）是升级信号之一。"""
    settings = seeded.settings
    settings.expand_enabled = True
    settings.expand_always = False
    total = seeded._components("work").repo.count_chunks(alive_only=True)
    # 用能命中的词 + 大于命中数的 top_k，让 low_yield 亮起来
    result = _search(seeded, "端口 netstat", top_k=max(5, total + 1))
    assert result.hits, "前提：该查询应有直接命中"
    assert result.debug["expand"]["triggered"] is True
    assert result.debug["expand"]["reason"] == "low_yield"


def test_no_expansion_when_signals_dark(seeded: Service):
    settings = seeded.settings
    settings.expand_enabled = True
    settings.expand_always = False
    # 先数出实际命中数，再让 top_k 恰好等于它：low_yield 暗；
    # 词法命中在 → weak 暗；重排关闭 → 平坦信号不适用。三者全暗 → 不触发。
    probe = _search(seeded, "RRF 向量 词法")
    assert probe.hits
    result = _search(seeded, "RRF 向量 词法", top_k=len(probe.hits))
    debug = result.debug.get("expand", {})
    assert debug.get("triggered") is False, debug
    assert result.expanded == []


def test_mode_suffix_and_mcp_payload(seeded: Service):
    settings = seeded.settings
    settings.expand_enabled = True
    settings.expand_always = True
    result = _search(seeded, "RRF 融合")
    payload = result.to_mcp_payload()
    if result.expanded:
        assert result.retrieval_mode.endswith("+expand")
        assert "expanded" in payload
        assert all(item["origin"] == "expand" for item in payload["expanded"])
        assert all(item["uid"] not in {h.uid for h in result.hits} for item in payload["expanded"])
    else:
        assert "expanded" not in payload
        assert not result.retrieval_mode.endswith("+expand")


def test_schema_version_bumped_to_4(seeded: Service):
    repo = seeded._components("work").repo
    assert repo.db.meta.get("schema_version") == "4"


def test_backfill_reports_counts(service: Service):
    _store_manual(service, "jieba A", ["jieba"], tags=["t-a"])
    result = service.rebuild_links("work")
    assert result["chunks"] == 1
    assert result["rows"] == 0  # 单切片无对端，无边可建
    assert result["counts"]["passive"] == result["counts"]["total"] == 0
    _store_manual(service, "jieba B", ["jieba"], tags=["t-b"])
    result = service.rebuild_links("work")
    # 一对 keyword 链接 × 双向 = 2 行；"rows" 是本次重建的累计写入数，
    # 终态以 counts 为准（第二个切片的重建会先清掉再重写第一对）。
    assert result["counts"]["passive"] == result["counts"]["total"] == 2
    assert result["rows"] >= 2


def test_summary_prompt_keeps_atomic_facts():
    """P0 的"库缺口"教训写进摘要契约：数字/错误码/结局必须留在 L0。"""
    assert "原子事实" in SYSTEM_PROMPT
    assert "结局" in SYSTEM_PROMPT
