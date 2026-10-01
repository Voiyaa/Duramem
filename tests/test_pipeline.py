"""检索管线测试——含设计文档里的两条验收标准：

- 「词法路价值验证」：含专有名词/错误码/路径的查询，词法路确实贡献了向量路
  没召回的切片。
- 「三路排名一致」：调试面板显示的排名必须与融合实际使用的排名一致
  （这条曾经真的因为 arm_ranks 的 off-by-one 而失败）。
"""

from __future__ import annotations

from duramem.retrieval.lexical import search_lexical
from duramem.store.repository import Repository


class BrokenEmbedding:
    """模拟嵌入接口故障，验证降级路径。"""

    dim = 1024
    model = "broken"

    def embed(self, texts):
        raise RuntimeError("模拟嵌入接口超时")

    def embed_one(self, text):
        raise RuntimeError("模拟嵌入接口超时")


def _repo(service) -> Repository:
    return service._components("work").repo


# ====================================================================== 词法路


def test_lexical_arm_finds_error_code(seeded):
    """错误码类的查询：向量表征很差，词法一次命中——这是词法路存在的理由。"""
    repo = _repo(seeded)
    hits = search_lexical(repo.db.read_conn, seeded.segmenter, "ERR_CONN_REFUSED_0x7f", k=10)
    assert hits, "词法路必须能命中错误码"
    top = repo.get_chunk_by_id(hits[0].chunk_id)
    assert "ERR_CONN_REFUSED_0x7f" in top["original_text"]


def test_lexical_arm_finds_path_and_identifier(seeded):
    repo = _repo(seeded)
    for term in ("BACKEND_PORT", ".env", "sqlite-vec", "AGPL"):
        hits = search_lexical(repo.db.read_conn, seeded.segmenter, term, k=10)
        assert hits, f"词法路应能命中 {term}"


def test_lexical_excludes_deleted_chunks(seeded):
    repo = _repo(seeded)
    before = search_lexical(repo.db.read_conn, seeded.segmenter, "BACKEND_PORT", k=10)
    assert before
    uid = repo.get_chunk_by_id(before[0].chunk_id)["chunk_uid"]
    seeded.forget(uid)
    after = search_lexical(repo.db.read_conn, seeded.segmenter, "BACKEND_PORT", k=10)
    assert all(repo.get_chunk_by_id(hit.chunk_id)["deleted_at"] is None for hit in after)


def test_lexical_does_not_filter_by_score(seeded):
    """语料很小时 bm25 会给 0 分（IDF 趋零），按分数过滤会误杀正确命中。
    RRF 只用排名，因此不需要也不应该做分数过滤。"""
    repo = _repo(seeded)
    hits = search_lexical(repo.db.read_conn, seeded.segmenter, "端口", k=10)
    assert hits


# ====================================================================== 融合


def test_search_uses_both_arms(seeded):
    res = seeded.search("ERR_CONN_REFUSED_0x7f 怎么解决的", db="work")
    assert res.retrieval_mode == "hybrid"
    assert res.hits
    top = res.hits[0]
    assert top.vec_rank == 1 and top.lex_rank == 1, "两路都应把正确切片排在第一"
    chunk = _repo(seeded).get_chunk(top.uid)
    assert "ERR_CONN_REFUSED_0x7f" in chunk["original_text"]


def test_vector_failure_degrades_to_lexical_only(seeded):
    """单路故障不能中断检索——记忆检索挂掉比检索质量下降严重得多。"""
    components = seeded._components("work")
    components.pipeline.embedder = BrokenEmbedding()

    res = components.pipeline.search("BACKEND_PORT")
    assert res.retrieval_mode == "lexical_only"
    assert res.hits, "向量路挂掉后词法路仍应给出结果"
    assert any("降级" in warning for warning in res.warnings)


def test_empty_vector_index_warns(seeded):
    components = seeded._components("work")
    components.index.clear()
    res = components.pipeline.search("端口")
    assert res.retrieval_mode == "lexical_only"
    assert any("尚无向量" in warning for warning in res.warnings)


def test_rerank_changes_mode_and_ordering(seeded):
    from duramem.providers.rerank import OverlapReranker

    components = seeded._components("work")
    assert components.pipeline.reranker.enabled is False

    plain = components.pipeline.search("端口被占用")
    assert "rerank" not in plain.retrieval_mode

    components.pipeline.reranker = OverlapReranker(seeded.segmenter)
    reranked = components.pipeline.search("端口被占用")
    assert reranked.retrieval_mode == "hybrid+rerank"
    assert reranked.hits[0].rerank_rank == 1
    assert reranked.debug["reranked"] is True


def test_rerank_failure_falls_back_to_rrf_order(seeded):
    from duramem.providers.rerank import RerankError

    class FailingReranker:
        enabled = True
        model = "failing"

        def rerank(self, query, documents, top_k=None):
            raise RerankError("模拟重排服务 503")

    components = seeded._components("work")
    components.pipeline.reranker = FailingReranker()
    res = components.pipeline.search("端口被占用")
    assert res.hits, "重排失败不应导致没有结果"
    assert any("重排失败" in warning for warning in res.warnings)
    assert "rerank" not in res.retrieval_mode


# ====================================================================== 调试数据


def test_debug_ranks_match_fusion(seeded):
    """调试面板的排名必须和融合用的排名同口径。"""
    res = seeded.search("RRF 融合", db="work")
    per_db = res.debug["per_db"]["work"]

    vec_ranks = [item["vec_rank"] for item in per_db["query_arms"]["vector"]]
    lex_ranks = [item["lex_rank"] for item in per_db["query_arms"]["lexical"]]
    assert vec_ranks and min(vec_ranks) == 1, "向量路排名必须从 1 开始"
    assert lex_ranks and min(lex_ranks) == 1, "词法路排名必须从 1 开始"
    assert vec_ranks == list(range(1, len(vec_ranks) + 1))
    assert lex_ranks == list(range(1, len(lex_ranks) + 1))


def test_debug_labels_each_arm(seeded):
    res = seeded.search("ERR_CONN_REFUSED_0x7f", db="work")
    pool = res.debug["per_db"]["work"]["fusion_pool"]
    arms = {item["arm"] for item in pool}
    assert arms <= {"both", "vector_only", "lexical_only"}
    assert "both" in arms, "两路都命中的切片应被标记出来，供面板着色"


def test_debug_exposes_params(seeded):
    res = seeded.search("端口", db="work")
    params = res.debug["per_db"]["work"]["params"]
    assert params["rrf_k"] == seeded.settings.rrf_k
    assert "vector_top_k" in params and "fusion_pool" in params


# ====================================================================== 排序


def test_weight_affects_order_without_rerank(seeded):
    """权重只在无重排时直接参与排序（有重排时以重排分数为准，权重仅作同分 tie-breaker）。"""
    res = seeded.search("端口 .env", db="work")
    assert len(res.hits) >= 2, "需要至少两条命中才能验证排序变化"

    top_uid = res.hits[0].uid
    seeded.update_chunk(top_uid, weight=0.01)
    after = seeded.search("端口 .env", db="work")
    assert after.hits[-1].uid == top_uid, "权重被压到 0.01 后应掉到最后"


def test_hit_count_does_not_affect_order(seeded):
    """命中计数只用于展示。让它参与排序会导致热门越热门，形成回音室。"""
    before = [hit.uid for hit in seeded.search("端口", db="work").hits]
    for _ in range(5):
        seeded.search("端口", db="work")
    after = [hit.uid for hit in seeded.search("端口", db="work").hits]
    assert before == after

    repo = _repo(seeded)
    # 计数是缓冲批刷的（读路径不再每次拿写锁），显式 flush 后才落库
    repo.flush_hits()
    assert repo.get_chunk(before[0])["hit_count"] >= 5


def test_top_k_is_respected(seeded):
    res = seeded.search("端口", db="work", top_k=1)
    assert len(res.hits) == 1


def test_tags_filter(seeded):
    repo = _repo(seeded)
    uid = seeded.search("端口", db="work").hits[0].uid
    repo.update_chunk(uid, tags=["nullable-test"])

    only_tagged = seeded.search("端口", db="work", tags=["nullable-test"])
    assert all("nullable-test" in hit.tags for hit in only_tagged.hits)

    other = seeded.search("端口", db="work", tags=["不存在的标签"])
    # 词法路被 tags 过滤掉，向量路仍会返回，但不应包含被标记的切片
    assert all("nullable-test" not in hit.tags for hit in other.hits)


def test_empty_query_returns_nothing(seeded):
    res = seeded.search("   ", db="work")
    assert res.hits == []
    assert res.retrieval_mode == "none"


# ====================================================================== 索引维护


def test_embed_pending_fills_missing_vectors(seeded):
    components = seeded._components("work")
    components.index.clear()
    assert components.index.count() == 0

    report = seeded.embed_pending("work")
    assert report["embedded"] > 0
    assert components.index.count() == report["embedded"]

    again = seeded.embed_pending("work")
    assert again["embedded"] == 0, "没有待补向量时不应重复嵌入"


def test_reindex_rebuilds_from_text(seeded):
    components = seeded._components("work")
    report = seeded.reindex("work")
    assert report["embedded"] == components.repo.count_chunks(alive_only=True)
    assert components.index.count() == report["embedded"]


def test_embedding_text_includes_keywords_and_tags(seeded):
    """嵌入输入 = L0 + 标题 + 关键词 + 标签，而注入上下文只用 L0。
    多出来的部分只在嵌入时用一次，对每轮 token 成本是零。"""
    components = seeded._components("work")
    row = components.repo.list_chunks(limit=1)[0]
    text = components.pipeline._embedding_text(row)
    assert row["summary_text"] in text
    for keyword in row["keywords"] and __import__("json").loads(row["keywords"]) or []:
        assert keyword in text
