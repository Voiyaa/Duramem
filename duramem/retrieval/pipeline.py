"""检索管线：向量 + 词法并行 → RRF 融合 → 可选重排 → 带元数据的结果。

设计文档 v2 §6。两个刻意的选择：

1. **两路都跑**，不存在"向量失败才启用词法"的判据。词法路的不可替代性在于
   专有名词、错误码、路径、变量名——这些词向量表征很差但词法一次命中。
2. **向量路失败不中断检索**，降级为纯词法并在 warnings 里说明。
   记忆检索挂掉比检索质量下降严重得多。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from duramem.config import Settings
from duramem.models import SearchHit
from duramem.providers.embedding import EmbeddingError, EmbeddingProvider
from duramem.providers.rerank import Reranker, RerankError
from duramem.retrieval.expand import expand as graph_expand
from duramem.retrieval.fusion import arm_ranks, ranked_ids, rrf_fuse
from duramem.retrieval.lexical import lex_hits_to_debug, search_lexical
from duramem.retrieval.sessions import SessionHit, SessionRetriever
from duramem.retrieval.vector_index import VectorIndex
from duramem.store.repository import Repository

OVERFETCH = 3

# 扩散增补的推荐语（给模型的判断依据）。关系名 → 中文说明。
_RELATION_LABELS = {
    "adjacent": "同会话相邻",
    "tag": "标签相连",
    "keyword": "关键词相连",
}


@dataclass
class RetrievalResult:
    hits: list[SearchHit] = field(default_factory=list)
    # 会话命中单独一段，不与切片混排。混排需要一个跨"目录/文件"的公共分数量纲，
     # 而两边的分数量纲本就不该可比——硬凑只会让排序变得不可解释。分开之后
     # "冷启动命中了一个会话"这件事本身就是清晰的信号。
    sessions: list[SessionHit] = field(default_factory=list)
    # 图扩散的增补结果（§16）：direct 命中之外的"可能相关"。与 hits 分开，
    # 道理与会话命中相同——PPR 分和重排分/RRF 分不同量纲，混排会毁掉排序的
    # 可解释性；分开之后"这是图连带出来的，不是查询直接打中的"本身就是信号。
    # 上层 Service 排序截断只动 hits，expanded 原样透传。
    expanded: list[SearchHit] = field(default_factory=list)
    retrieval_mode: str = "none"
    warnings: list[str] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)
    suggestion: str | None = None

    def to_mcp_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "results": [hit.to_mcp_dict() for hit in self.hits],
            "retrieval_mode": self.retrieval_mode,
            "count": len(self.hits),
        }
        if self.expanded:
            payload["expanded"] = [hit.to_mcp_dict() for hit in self.expanded]
        if self.sessions:
            payload["sessions"] = [hit.to_mcp_dict() for hit in self.sessions]
            payload["session_count"] = len(self.sessions)
        if self.warnings:
            payload["warnings"] = self.warnings
        if self.suggestion:
            payload["_suggestion"] = self.suggestion
        return payload


class RetrievalPipeline:
    """单库检索。跨库调度在上层 Service 里做。"""

    def __init__(
        self,
        repo: Repository,
        embedder: EmbeddingProvider,
        reranker: Reranker,
        index: VectorIndex,
        settings: Settings,
        db_name: str | None = None,
        session_retriever: SessionRetriever | None = None,
    ) -> None:
        self.repo = repo
        self.embedder = embedder
        self.reranker = reranker
        self.index = index
        self.settings = settings
        self.db_name = db_name
        # 会话检索器可选：没有它就退化成纯切片检索（老库、或只关心切片的调用方）
        self.session_retriever = session_retriever

    # ================================================================== 入口

    def search(
        self,
        query: str,
        top_k: int | None = None,
        tags: Sequence[str] | None = None,
        collect_debug: bool = True,
    ) -> RetrievalResult:
        query = (query or "").strip()
        limit = top_k or self.settings.rerank_top_k
        warnings: list[str] = []

        if not query:
            return RetrievalResult(warnings=["查询为空"], retrieval_mode="none")

        # ---------------------------------------------------------- 向量路
        #
        # 查询向量只算一次，切片路与会话路共用（同一个 query 文本、同一个嵌入
        # 提供方）。曾经两路各嵌一遍，每次检索凭空多付一趟模型 API 往返——
        # 那是检索延迟里最大的纯浪费。嵌入失败时会话路仍自嵌一次（原行为，
        # 瞬时抖动下两路独立重试）；KNN 失败不影响把向量交给会话路。
        vector_hits: list[tuple[int, float]] = []
        dropped_by_floor = 0
        query_vector: list[float] | None = None
        try:
            query_vector = self.embedder.embed_one(query)
        except EmbeddingError as exc:
            warnings.append(f"向量检索不可用，已降级为词法检索：{exc}")
        except Exception as exc:  # noqa: BLE001 - 检索不该因单路故障而失败
            warnings.append(f"向量检索异常，已降级为词法检索：{exc}")

        if query_vector is not None:
            try:
                raw = self.index.search(query_vector, self.settings.vector_top_k * OVERFETCH)
                kept, dropped_by_floor = self._apply_distance_floor(raw)
                vector_hits = self._filter_alive(kept)
                if tags:
                    vector_hits = self._filter_tags(vector_hits, tags)
                vector_hits = vector_hits[: self.settings.vector_top_k]
            except Exception as exc:  # noqa: BLE001 - 检索不该因单路故障而失败
                warnings.append(f"向量检索异常，已降级为词法检索：{exc}")

        if not vector_hits and not warnings:
            total_vectors = self.index.count()
            if total_vectors == 0:
                warnings.append("本库尚无向量，本次仅做词法检索。可执行 duramem reindex 补齐。")
            elif dropped_by_floor:
                warnings.append(
                    f"向量路有 {dropped_by_floor} 条候选因相似度低于下限"
                    f"（余弦距离 > {self.settings.vector_max_distance}）被剔除。"
                )

        # ---------------------------------------------------------- 词法路
        lexical_hits = search_lexical(
            self.repo.db.read_conn,
            self.repo.segmenter,
            query,
            k=self.settings.lexical_top_k,
            tags=tags,
        )

        # ---------------------------------------------------------- 融合
        rankings = {
            "vector": [chunk_id for chunk_id, _ in vector_hits],
            "lexical": [hit.chunk_id for hit in lexical_hits],
        }
        fused = rrf_fuse(rankings, k=self.settings.rrf_k)
        if not fused:
            result = RetrievalResult(
                warnings=warnings or ["未命中任何记忆切片"],
                retrieval_mode=self._mode(bool(vector_hits), bool(lexical_hits), False),
            )
            if collect_debug:
                result.debug = self._debug_payload(rankings, vector_hits, lexical_hits, [], [])
            return result

        pool_ids = ranked_ids(fused)[: self.settings.fusion_pool]
        rows = self.repo.get_chunks_by_ids(pool_ids)

        # ---------------------------------------------------------- 可选重排
        reranked = False
        rerank_scores: dict[int, float] = {}
        rerank_order: list[int] = []
        pool_rows = [rows[cid] for cid in pool_ids if cid in rows]

        if self.reranker.enabled and pool_rows:
            try:
                # 重排输入只用 L0 + 标题；不注入 keywords，避免关键词堆砌拉高分数
                documents = [
                    (
                        f"{row['title']}\n{row['summary_text']}"
                        if row["title"]
                        else row["summary_text"]
                    )
                    for row in pool_rows
                ]
                pairs = self.reranker.rerank(query, documents, top_k=None)
                reranked = True
                rerank_order = [pool_rows[position]["id"] for position, _ in pairs]
                rerank_scores = {pool_rows[position]["id"]: score for position, score in pairs}
            except RerankError as exc:
                warnings.append(f"重排失败，已按 RRF 排序返回：{exc}")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"重排异常，已按 RRF 排序返回：{exc}")

        # ---------------------------------------------------------- 排序与截断
        vec_rank = arm_ranks(rankings["vector"])
        lex_rank = arm_ranks(rankings["lexical"])

        if reranked:
            ordered_ids = rerank_order
        else:
            ordered_ids = pool_ids

        ordered_ids = [cid for cid in ordered_ids if cid in rows][:limit]

        hits: list[SearchHit] = []
        for position, cid in enumerate(ordered_ids, start=1):
            row = rows[cid]
            hit = self._build_hit(
                row=row,
                cid=cid,
                rrf_score=fused.get(cid, 0.0),
                vec_rank=vec_rank.get(cid),
                vec_distance=dict(vector_hits).get(cid),
                lex_rank=lex_rank.get(cid),
                lex_score=next(
                    (h.score for h in lexical_hits if h.chunk_id == cid), None
                ),
                rerank_rank=position if reranked else None,
                rerank_score=rerank_scores.get(cid),
            )
            hits.append(hit)

        self._apply_suggestions(hits, rows, warnings)
        weak_warned = self._warn_if_weak(hits, warnings)

        # 命中计数：仅展示用，不参与排序
        try:
            self.repo.bump_hits([hit.uid for hit in hits])
        except Exception:  # noqa: BLE001 - 计数失败不影响检索
            pass

        # ---------------------------------------------------------- 图扩散（§16）
        #
        # 迭代检索环路的"自动联想"层：从直接命中沿 links 图做 PPR，把多跳相连、
        # 查询词够不着的切片**增补**进返回。三条不变式：
        # 1. 直接命中一个字都不动（增补不替换）——关掉开关或信号未亮时，
        #    本函数的返回与 v3 完全一致（§16.10 的零回归验收就在这条上）；
        # 2. 只在升级信号亮时跑：产出不足 / 弱结果告警 / 重排分平坦。
        #    RRF 分天然挤在小区间（那是 rank 融合的数学性质），不能当平坦信号用；
        # 3. 扩散失败只降级为无增补，绝不拖垮检索。
        expanded: list[SearchHit] = []
        expand_debug: dict[str, Any] = {}
        if hits and self.settings.expand_enabled:
            triggered, reason = self._expand_trigger(hits, limit, reranked, weak_warned)
            if self.settings.expand_always or triggered:
                expanded, expand_debug = self._expand_hits(
                    hits, warnings, reason or "always"
                )
            else:
                expand_debug = {"triggered": False, "reason": reason}

        mode = self._mode(bool(vector_hits), bool(lexical_hits), reranked)
        if expanded:
            mode = f"{mode}+expand"

        # ---------------------------------------------------------- 会话路
        #
        # 独立于切片路：切片召回要求查询里有词，而"我们接着上次那个来"没有词。
        # 会话层命中不参与切片排序，单独成段返回（见 RetrievalResult 的说明）。
        sessions: list[SessionHit] = []
        session_debug: dict[str, Any] = {}
        if self.session_retriever is not None:
            try:
                # 只跑一次：debug 与结果来自同一次检索，重跑一遍会白花一次
                # 嵌入 + 可能的模型重排，还让调试面板显示的排名与实际返回不一致。
                session_result = self.session_retriever.search(
                    query, collect_debug=collect_debug, query_vector=query_vector
                )
                sessions = session_result.hits
                session_debug = session_result.debug
                warnings.extend(session_result.warnings)
            except Exception as exc:  # noqa: BLE001 - 会话路失败不该拖垮切片检索
                warnings.append(f"会话检索失败，已只返回切片：{exc}")

        result = RetrievalResult(
            hits=hits,
            sessions=sessions,
            expanded=expanded,
            retrieval_mode=mode,
            warnings=warnings,
            suggestion=self._global_suggestion(hits),
        )
        if collect_debug:
            result.debug = self._debug_payload(
                rankings, vector_hits, lexical_hits, pool_ids, ordered_ids, rerank_scores, reranked
            )
            if session_debug:
                result.debug.update(session_debug)
            if expand_debug:
                result.debug["expand"] = expand_debug
        return result

    # ================================================================== 内部

    def _apply_distance_floor(
        self, hits: list[tuple[int, float]]
    ) -> tuple[list[tuple[int, float]], int]:
        """剔除余弦距离超过下限的候选。

        为什么需要：向量 KNN 永远会返回 k 条结果，哪怕库里没有任何相关内容。
        没有下限时，一个完全无关的查询也会拿到 5 条"看起来有分数"的记忆，
        模型无法区分"有相关记忆"和"什么都没有"——而它应该能说出"没检索到"。

        下限刻意宽松（默认 0.9 ≈ 余弦相似度 0.1），只剔除近似正交的结果。
        收紧它会与具体嵌入模型强绑定，所以只留一个可调的旋钮，不做通用常数。
        """
        floor = self.settings.vector_max_distance
        if floor is None or floor <= 0:
            return hits, 0
        kept = [(cid, dist) for cid, dist in hits if dist <= floor]
        return kept, len(hits) - len(kept)

    def _filter_tags(
        self, hits: list[tuple[int, float]], tags: Sequence[str]
    ) -> list[tuple[int, float]]:
        """按标签过滤向量候选。

        tags 必须对两路一致生效——否则向量路会把用户明确排除的切片带回来。
        """
        wanted = set(tags)
        rows = self.repo.get_chunks_by_ids([cid for cid, _ in hits])
        out: list[tuple[int, float]] = []
        for cid, distance in hits:
            row = rows.get(cid)
            if row is None:
                continue
            try:
                row_tags = set(json.loads(row["tags"] or "[]"))
            except (TypeError, ValueError):
                row_tags = set()
            if wanted <= row_tags:
                out.append((cid, distance))
        return out

    def _filter_alive(self, hits: list[tuple[int, float]]) -> list[tuple[int, float]]:
        """软删除/被取代的切片不参与召回。向量保留以便恢复，故在检索侧过滤。"""
        if not hits:
            return []
        rows = self.repo.get_chunks_by_ids([cid for cid, _ in hits])
        alive: list[tuple[int, float]] = []
        for chunk_id, distance in hits:
            row = rows.get(chunk_id)
            if row is None:
                continue
            if row["deleted_at"] is not None or row["superseded_by"] is not None:
                continue
            alive.append((chunk_id, distance))
        return alive

    def _build_hit(
        self,
        row: dict[str, Any],
        cid: int,
        rrf_score: float,
        vec_rank: int | None,
        vec_distance: float | None,
        lex_rank: int | None,
        lex_score: float | None,
        rerank_rank: int | None,
        rerank_score: float | None,
    ) -> SearchHit:
        weight = float(row["weight"] or 1.0)
        if rerank_score is not None:
            score = float(rerank_score)
        else:
            score = rrf_score * weight

        tags = _load_json_list(row["tags"])
        keywords = _load_json_list(row["keywords"])
        # "还有更深一层可读"的信号。三层之后它指的是**原文（L2）**是否可取——
        # 模型的判断链是 L0 → 会话 L1 → 切片 L2，而 L1 走会话 id 不在这里暴露，
        # 所以这个布尔只回答"要不要去读原文"。
        has_original = bool(row["original_text"]) and self.settings.original_access_enabled

        return SearchHit(
            uid=row["chunk_uid"],
            chunk_id=cid,
            title=row["title"] or "",
            summary_text=row["summary_text"],
            score=score,
            tags=tags,
            keywords=keywords,
            weight=weight,
            hit_count=int(row["hit_count"] or 0),
            has_original=has_original,
            session_id=row["source_session"],
            msg_id_start=row["msg_id_start"],
            msg_id_end=row["msg_id_end"],
            rrf_score=rrf_score,
            vec_rank=vec_rank,
            vec_distance=vec_distance,
            lex_rank=lex_rank,
            lex_score=lex_score,
            rerank_rank=rerank_rank,
            rerank_score=rerank_score,
            db=self.db_name,
        )

    def _apply_suggestions(
        self, hits: list[SearchHit], rows: dict[int, dict[str, Any]], warnings: list[str]
    ) -> None:
        """给模型"该不该看原文"的判断依据（设计文档 §6 要点 6）。

        没有这个信号，R5 会变成一个模型永远不会用的功能——
        它不知道摘要是否够用、原文是否存在、值不值得再调一次工具。
        """
        for hit in hits:
            reasons: list[str] = []
            if hit.has_original:
                if hit.msg_id_start is not None:
                    reasons.append("可用 dm_read_original(uid, mode='window') 取原文及相邻消息")
                else:
                    reasons.append("可用 dm_read_original(uid, mode='full') 取原文")
            row = rows.get(hit.chunk_id) or {}
            if int(row.get("summary_truncated") or 0) == 1:
                reasons.append("该摘要已达长度上限，细节可能被压缩")
            # 会话入口：分层设计里,"想知道整体脉络或该看哪一条"要先取会话概览。
            # 提示只在开关打开时给——否则模型会去调一个必然被拒的工具。
            if hit.session_id and self.settings.overview_access_enabled:
                reasons.append(
                    f"想知道所在会话的整体脉络可用 dm_read_session('{hit.session_id}')"
                )
            if hit.msg_id_start is not None and hit.msg_id_end is not None:
                reasons.append("存在相邻切片时可用 dm_read_neighbors 补全上下文")
            hit.suggestion = "；".join(reasons) if reasons else None

    def _warn_if_weak(self, hits: list[SearchHit], warnings: list[str]) -> bool:
        """两路都没有自信命中时明确说出来。

        模型需要能区分"有相关记忆"和"只是在勉强返回最近的几条"。不给这个信号，
        它会拿一堆弱相关摘要硬答——这正是幻觉的来源之一。
        返回值同时是图扩散的升级信号之一（weak_results）。
        """
        if not hits:
            return False
        weak = self.settings.vector_weak_distance
        has_lexical = any(hit.lex_rank is not None for hit in hits)
        if has_lexical:
            return False
        distances = [hit.vec_distance for hit in hits if hit.vec_distance is not None]
        if distances and min(distances) > weak:
            warnings.append(
                f"本次结果相关性普遍偏低（最近一条余弦距离 {min(distances):.3f} > "
                f"{weak}），词法路也没有命中。很可能库里没有与该问题相关的记忆——"
                "如果检索结果不足以回答，请直接说明没找到，不要勉强作答。"
            )
            return True
        return False

    def _expand_trigger(
        self, hits: list[SearchHit], limit: int, reranked: bool, weak_warned: bool
    ) -> tuple[bool, str | None]:
        """升级信号：要不要为这次查询付出扩散的 token。

        P0 评测（eval/p0/results-2026-09-28.md）的教训在这里落了地：
        "整题感觉够答"不可靠，信号必须是可计算的。
        """
        if len(hits) < limit:
            return True, "low_yield"
        if weak_warned:
            return True, "weak_results"
        # 重排分平坦 = 检索器在几个候选之间"猜"。注意只在重排开启时判：
        # RRF 分数天然挤在小区间（rank 融合的性质，见 S3 会话的实测 0.0287–0.0325），
        # 拿它判平坦永远为真，等于常开扩散。
        if reranked and len(hits) >= 2:
            scores = [hit.score for hit in hits]
            top = max(scores)
            if top > 0 and (top - min(scores)) / top < 0.2:
                return True, "flat_scores"
        return False, None

    def _expand_hits(
        self, hits: list[SearchHit], warnings: list[str], reason: str
    ) -> tuple[list[SearchHit], dict[str, Any]]:
        """跑 PPR 扩散并把候选装配成 SearchHit（origin=expand）。"""
        seed_weights = {hit.chunk_id: max(float(hit.score), 1e-6) for hit in hits}
        seeds_payload = [
            {"uid": hit.uid, "score": round(float(hit.score), 4)} for hit in hits
        ]
        try:
            candidates = graph_expand(
                self.repo.db.read_conn, seed_weights, max(0, self.settings.expand_top_k)
            )
        except Exception as exc:  # noqa: BLE001 - 扩散失败不拖垮检索
            warnings.append(f"图扩散失败，已只返回直接命中：{exc}")
            return [], {
                "triggered": True,
                "reason": reason,
                "seeds": seeds_payload,
                "error": str(exc),
            }

        if not candidates:
            return [], {
                "triggered": True,
                "reason": reason,
                "seeds": seeds_payload,
                "emitted": [],
                "note": "seeds 没有任何可用链接（图尚未生成，可执行 duramem links-rebuild）",
            }

        rows = self.repo.get_chunks_by_ids([c.chunk_id for c in candidates])
        seed_uids = {hit.chunk_id: hit.uid for hit in hits}
        out: list[SearchHit] = []
        emitted: list[dict[str, Any]] = []
        for cand in candidates:
            row = rows.get(cand.chunk_id)
            if row is None:
                continue
            hit = self._build_hit(
                row=row,
                cid=cand.chunk_id,
                rrf_score=0.0,
                vec_rank=None,
                vec_distance=None,
                lex_rank=None,
                lex_score=None,
                rerank_rank=None,
                rerank_score=None,
            )
            hit.score = round(cand.score, 4)
            hit.origin = "expand"
            hit.expand_score = hit.score
            hit.expand_via = seed_uids.get(cand.seed_id)
            hit.expand_relation = cand.relation
            hit.expand_hops = cand.hops
            label = _RELATION_LABELS.get(cand.relation, cand.relation)
            via = hit.expand_via or f"chunk#{cand.seed_id}"
            hit.suggestion = (
                f"该切片不是查询直接命中，而是由图扩散补充（经由 {via} 的{label}链接、"
                f"{cand.hops} 跳）；它与上面的直接命中相关，可作为补充上下文"
            )
            out.append(hit)
            emitted.append(
                {
                    "uid": hit.uid,
                    "chunk_id": cand.chunk_id,
                    "ppr": round(cand.ppr, 6),
                    "score": hit.score,
                    "hops": cand.hops,
                    "relation": cand.relation,
                    "via_seed": hit.expand_via,
                    "path": cand.path,
                }
            )
        return out, {
            "triggered": True,
            "reason": reason,
            "seeds": seeds_payload,
            "emitted": emitted,
        }

    def _global_suggestion(self, hits: list[SearchHit]) -> str | None:
        if not hits:
            return "未命中记忆。可换用更具体的关键词或专有名词重试。"
        if any(hit.has_original for hit in hits):
            return (
                "以上为记忆摘要。若问题涉及具体数值、原话、错误码或文件名，"
                "摘要可能不足以回答，请用 dm_read_original 查看原文。"
            )
        return None

    def _mode(self, has_vector: bool, has_lexical: bool, reranked: bool) -> str:
        if has_vector and has_lexical:
            mode = "hybrid"
        elif has_vector:
            mode = "vector_only"
        elif has_lexical:
            mode = "lexical_only"
        else:
            return "none"
        return f"{mode}+rerank" if reranked else mode

    def _debug_payload(
        self,
        rankings: dict[str, list[int]],
        vector_hits: list[tuple[int, float]],
        lexical_hits: list[Any],
        pool_ids: list[int],
        ordered_ids: list[int],
        rerank_scores: dict[int, float] | None = None,
        reranked: bool = False,
    ) -> dict[str, Any]:
        """检索调试面板的数据源：三路排名全暴露。

        这是自己实现融合（而非用 zvec 之类的一体化引擎）的直接收益——
        一体化引擎把融合做在内部，中间排名就看不到了。
        """
        all_ids = {cid for cid in rankings["vector"]} | {h.chunk_id for h in lexical_hits}
        rows = self.repo.get_chunks_by_ids(sorted(all_ids))
        vec_rank = arm_ranks(rankings["vector"])
        lex_rank = arm_ranks(rankings["lexical"])

        def describe(cid: int) -> dict[str, Any]:
            row = rows.get(cid, {})
            arm = (
                "both"
                if cid in vec_rank and cid in lex_rank
                else "vector_only"
                if cid in vec_rank
                else "lexical_only"
            )
            return {
                "chunk_id": cid,
                "uid": row.get("chunk_uid"),
                "title": row.get("title"),
                "summary": row.get("summary_text"),
                "arm": arm,
                "vec_rank": vec_rank.get(cid),
                "lex_rank": lex_rank.get(cid),
            }

        return {
            "query_arms": {
                "vector": [describe(cid) for cid in rankings["vector"]],
                "lexical": [describe(hit.chunk_id) for hit in lexical_hits],
            },
            "vector": [
                {
                    "rank": index + 1,
                    "chunk_id": cid,
                    "distance": round(distance, 6),
                    **{k: v for k, v in describe(cid).items() if k not in {"vec_rank"}},
                }
                for index, (cid, distance) in enumerate(vector_hits)
            ],
            "lexical": lex_hits_to_debug(self.repo.db.read_conn, lexical_hits),
            "fusion_pool": [describe(cid) for cid in pool_ids],
            "final_order": ordered_ids,
            "reranked": reranked,
            "params": {
                "rrf_k": self.settings.rrf_k,
                "vector_top_k": self.settings.vector_top_k,
                "lexical_top_k": self.settings.lexical_top_k,
                "fusion_pool": self.settings.fusion_pool,
                "rerank_top_k": self.settings.rerank_top_k,
            },
        }

    # ================================================================== 维护

    def embed_pending(self, limit: int = 256) -> dict[str, Any]:
        """给缺向量的切片补算向量（首次导入、或嵌入曾失败时使用）。"""
        pending = self.repo.chunks_without_vectors()[:limit]
        if not pending:
            return {"embedded": 0, "remaining": 0}

        texts = [self._embedding_text(row) for row in pending]
        vectors = self.embedder.embed(texts)
        # strict=True：嵌入器契约是"一条文本一个向量"，条数不齐就是坏了——
        # 静默对齐会把向量写错切片，宁可当场炸（大声拒绝原则）。
        self.index.add_many(
            [(int(row["id"]), vector) for row, vector in zip(pending, vectors, strict=True)]
        )
        return {
            "embedded": len(pending),
            "remaining": max(0, len(self.repo.chunks_without_vectors())),
        }

    def reindex_all(self) -> dict[str, Any]:
        """全量重建向量索引（换模型 / 换嵌入输入后使用）。

        会话层的向量也要一起重建。`rebuild_vectors` 会把两张向量表一起删掉重建
        （它们用同一个嵌入提供方，只换一张会让库里混两种来源的向量），所以
        只重建切片向量的话，会话向量会一直空着、冷启动那条路静默失效。
        """
        candidates = [self.repo.get_chunk_by_id(cid) for cid in self.repo.alive_ids()]
        rows: list[dict[str, Any]] = [row for row in candidates if row is not None]
        if not rows:
            self.index.clear()
            sessions = self._reindex_sessions()
            return {"embedded": 0, "sessions_embedded": sessions}

        vectors = self.embedder.embed([self._embedding_text(row) for row in rows])
        count = self.index.rebuild(
            [(int(row["id"]), vector) for row, vector in zip(rows, vectors, strict=True)]
        )
        return {"embedded": count, "sessions_embedded": self._reindex_sessions()}

    def _reindex_sessions(self) -> int:
        """重建会话层向量。没有会话检索器时返回 0。"""
        if self.session_retriever is None:
            return 0
        retriever = self.session_retriever
        layers = retriever.store.list(limit=10_000)
        layers = [row for row in layers if str(row.get("abstract_text") or "").strip()]
        retriever.index.clear()
        if not layers:
            return 0
        from duramem.session_summarizer import session_embedding_text

        vectors = self.embedder.embed([session_embedding_text(row) for row in layers])
        return retriever.index.rebuild(
            [(int(row["id"]), vector) for row, vector in zip(layers, vectors, strict=True)]
        )

    def _embedding_text(self, row: dict[str, Any]) -> str:
        """嵌入输入 = 摘要 + 关键词 + 标签（设计文档 §3.4）。

        与注入上下文用的文本刻意分开：多出来的关键词/标签只在嵌入时使用一次，
        不进上下文，因此对每轮的 token 成本是零。收益是专有名词、错误码、
        项目代号能进入向量空间。
        """
        keywords = _load_json_list(row["keywords"])
        tags = _load_json_list(row["tags"])
        parts = [row["summary_text"]]
        if row["title"]:
            parts.append(row["title"])
        if keywords:
            parts.append(" ".join(keywords))
        if tags:
            parts.append(" ".join(tags))
        return " ".join(part for part in parts if part)


def _load_json_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


__all__ = ["RetrievalPipeline", "RetrievalResult", "OVERFETCH"]
