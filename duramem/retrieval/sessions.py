"""会话层检索：让"我们接着上次那个来"这类无关键词的查询能命中会话。

为什么需要单独一条检索路：切片召回的**前提是查询里有词**。新窗口第一句
"我们接着上次那个来"没有任何可召回的词，模型处于失忆开场——这正是当年设计
`is_session_summary` 想补的缺口。会话层的 L0 摘要进向量与词法索引之后，这类查询
可以凭语义命中会话本身。

分工照 OpenViking：**L0 管召回、L1 管精排**。会话记录的精排打分体是 L1 概览
（不是 L0）——L1 里有导航段与逐切片小节，判别力强得多。切片记录的精排仍用
自己的 L0：两边的打分体都是"记录自己的正文"，所以同一会话下的多个切片不会
因为共用一份概览而打分并列（那是把 L1 当成全局打分体时会踩的坑）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from duramem.config import Settings
from duramem.providers.rerank import Reranker
from duramem.reader import truncate_to_tokens
from duramem.retrieval.fusion import ranked_ids, rrf_fuse
from duramem.retrieval.vector_index import VectorIndex
from duramem.store.session_layers import SessionLayerStore
from duramem.text.tokenize import Segmenter

# 与切片检索一致的过采样倍数：先多取一些候选，过滤后再截断
OVERFETCH = 3


class Embedder(Protocol):
    def embed_one(self, text: str) -> list[float]: ...


@dataclass
class SessionHit:
    """一条会话命中。

    刻意**只带 L0 不带 L1 正文**：L1 有 4000 token，随每个命中返回会在
    会话数一多时把每轮上下文灌爆，而照 OvK 它本该由模型按需去取。
    模型拿到 `session_id` 与这段 L0 摘要，就能判断要不要深入。
    """

    session_id: str
    window_id: str
    abstract_text: str
    slice_count: int = 0
    score: float = 0.0
    rrf_score: float = 0.0
    vec_rank: int | None = None
    vec_distance: float | None = None
    lex_rank: int | None = None
    lex_score: float | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None
    db: str | None = None

    def to_mcp_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "abstract": self.abstract_text,
            "slice_count": self.slice_count,
            "score": round(self.score, 4),
        }
        if self.db:
            payload["db"] = self.db
        payload["_suggestion"] = (
            f"这是会话级概览入口。需要这段会话的整体脉络或该看哪条切片时，"
            f"调用 dm_read_session(session_id='{self.session_id}')。"
        )
        return payload


@dataclass
class SessionSearchResult:
    hits: list[SessionHit] = field(default_factory=list)
    retrieval_mode: str = "none"
    warnings: list[str] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)


def rerank_document(overview_text: str, max_tokens: int) -> str:
    """会话重排的打分体：L1 概览的**前若干 token**。

    为什么不整段送：L1 上限 1600 token，而重排要对每个候选各送一份
    （典型 3-5 个候选就是几千 token/次查询）。L1 精简后（标题/简述/覆盖度/
    导航，详述节已删——见 session_summarizer 与设计文档 A.16），判别力最强
    的三段——简述、覆盖度、导航——仍在最前面，1200 的截断覆盖它们与导航
    的前段；尾部导航行是最不具判别力的部分。曾经的逐切片详述小节已从 L1
    删除（它是 L0 的派生冗余），截断不再丢失独有内容。

    独立成模块函数而不是方法：它不依赖任何实例状态，单测不必拼凑一个检索器。
    """
    text, _ = truncate_to_tokens(str(overview_text or ""), max_tokens)
    return text


def _lexical_sessions(
    conn, segmenter: Segmenter, query: str, k: int
) -> list[tuple[int, float]]:
    """会话层的 FTS5 检索。返回 [(layer_id, bm25)]，按 bm25 升序。"""
    match_expr = segmenter.segment_query(query)
    if not match_expr or k <= 0:
        return []
    sql = (
        "SELECT f.rowid AS layer_id, bm25(session_layers_fts) AS score "
        "FROM session_layers_fts f JOIN session_layers l ON l.id = f.rowid "
        "WHERE f.search_text MATCH ? "
        "ORDER BY score LIMIT ?"
    )
    rows = conn.execute(sql, (match_expr, k)).fetchall()
    return [(int(r["layer_id"]), float(r["score"])) for r in rows]


class SessionRetriever:
    """会话层的两路检索 + RRF 融合 + 可选重排。"""

    def __init__(
        self,
        store: SessionLayerStore,
        embedder: Embedder,
        reranker: Reranker,
        index: VectorIndex,
        settings: Settings,
        db_name: str = "",
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.index = index
        self.settings = settings
        self.db_name = db_name
        self.segmenter = store.segmenter

    def search(
        self,
        query: str,
        top_k: int | None = None,
        collect_debug: bool = False,
        query_vector: Sequence[float] | None = None,
    ) -> SessionSearchResult:
        """`query_vector` 传入时直接复用切片路已算好的查询向量。

        两路嵌的是同一个 query 文本、同一个嵌入提供方（见 `Service._components`），
        各嵌一遍是纯浪费的一趟模型 API 往返。传 None（独立调用方）才自己嵌。
        """
        limit = self.settings.session_top_k if top_k is None else max(0, int(top_k))
        if limit <= 0 or not (query or "").strip():
            return SessionSearchResult()

        warnings: list[str] = []
        debug: dict[str, Any] = {}
        modes: list[str] = []

        # ---- 向量路
        vec_pairs: list[tuple[int, float]] = []
        vec_ranks: dict[int, int] = {}
        try:
            vector = (
                list(query_vector) if query_vector is not None else self.embedder.embed_one(query)
            )
            fetched = self.index.search(vector, max(1, limit * OVERFETCH))
            # 与会话层的相关性下限：距离 > 阈值的一律不要，让"没检索到"能被说出来，
            # 而不是硬凑 k 条。这与切片检索用的是同一个阈值——两边都是余弦距离。
            kept = [
                (cid, dist)
                for cid, dist in fetched
                if dist <= self.settings.vector_max_distance
            ]
            vec_pairs = kept[: max(1, limit * OVERFETCH)]
            vec_ranks = {cid: i + 1 for i, (cid, _) in enumerate(vec_pairs)}
            modes.append("vector")
        except Exception as exc:  # noqa: BLE001 - 向量路失败要降级而不是整体失败
            warnings.append(f"会话向量检索不可用，已降级为词法：{exc}")

        # ---- 词法路
        lex_pairs = _lexical_sessions(
            self.store.db.read_conn,
            self.segmenter,
            query,
            max(1, limit * OVERFETCH),
        )
        lex_ranks = {cid: i + 1 for i, (cid, _) in enumerate(lex_pairs)}
        if lex_pairs:
            modes.append("lexical")

        if not vec_pairs and not lex_pairs:
            return SessionSearchResult(warnings=warnings, retrieval_mode="none")

        # ---- 融合。两路都按 rank 参与，与会话的 size 无关
        fused = rrf_fuse(
            {
                "vector": [cid for cid, _ in vec_pairs],
                "lexical": [cid for cid, _ in lex_pairs],
            },
            k=self.settings.rrf_k,
        )
        pool_ids = ranked_ids(fused)[: max(1, limit * OVERFETCH)]
        if not pool_ids:
            return SessionSearchResult(warnings=warnings, retrieval_mode="+".join(modes))

        rows = {
            int(r["id"]): dict(r)
            for r in self.store.db.read_conn.execute(
                f"SELECT * FROM session_layers WHERE id IN ({','.join('?' * len(pool_ids))})",
                pool_ids,
            ).fetchall()
        }

        # ---- 可选重排：打分体是 **L1 概览**，不是 L0
        rerank_ranks: dict[int, int] = {}
        rerank_scores: dict[int, float] = {}
        if self.reranker.enabled and rows:
            documents: list[str] = []
            order: list[int] = []
            for layer_id in pool_ids:
                row = rows.get(layer_id)
                if row is None:
                    continue
                order.append(layer_id)
                documents.append(
                    rerank_document(
                        str(row["overview_text"] or ""),
                        self.settings.session_rerank_max_tokens,
                    )
                )
            try:
                for index, score in self.reranker.rerank(query, documents):
                    rerank_ranks[order[index]] = len(rerank_ranks) + 1
                    rerank_scores[order[index]] = float(score)
                modes.append("rerank")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"会话重排失败，已按融合分排序：{exc}")

        hits: list[SessionHit] = []
        for layer_id in pool_ids:
            row = rows.get(layer_id)
            if row is None:
                continue
            rrf_score = float(fused.get(layer_id, 0.0))
            rerank_rank = rerank_ranks.get(layer_id)
            hits.append(
                SessionHit(
                    session_id=str(row["session_id"]),
                    window_id=str(row["window_id"]),
                    abstract_text=str(row["abstract_text"] or ""),
                    slice_count=int(row["coverage_total"] or 0),
                    score=(
                        rerank_scores[layer_id] if rerank_rank is not None else rrf_score
                    ),
                    rrf_score=rrf_score,
                    vec_rank=vec_ranks.get(layer_id),
                    vec_distance=dict(vec_pairs).get(layer_id),
                    lex_rank=lex_ranks.get(layer_id),
                    lex_score=dict(lex_pairs).get(layer_id),
                    rerank_rank=rerank_rank,
                    rerank_score=rerank_scores.get(layer_id),
                    db=self.db_name,
                )
            )

        if rerank_ranks:
            hits.sort(key=lambda h: (h.rerank_rank or 0, -h.score))
        else:
            hits.sort(key=lambda h: (-h.score, h.session_id))
        hits = hits[:limit]

        if collect_debug:
            debug = {
                "session_vector": [cid for cid, _ in vec_pairs],
                "session_lexical": [cid for cid, _ in lex_pairs],
                "session_fusion_pool": pool_ids,
                "session_reranked": [
                    cid for cid, _ in sorted(rerank_ranks.items(), key=lambda p: p[1])
                ],
            }

        return SessionSearchResult(
            hits=hits,
            retrieval_mode="+".join(modes),
            warnings=warnings,
            debug=debug,
        )

__all__ = [
    "SessionHit",
    "SessionRetriever",
    "SessionSearchResult",
    "rerank_document",
]
