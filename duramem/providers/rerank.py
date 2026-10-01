"""重排模型提供方。可选，默认关闭。

关闭时 `NullReranker` 原样返回——降级路径必须完整可用，
不能出现"关了重排就查不出东西"的情况。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import httpx

from duramem import net
from duramem.text.tokenize import Segmenter, get_segmenter


class RerankError(RuntimeError):
    pass


class Reranker(Protocol):
    enabled: bool
    model: str

    def rerank(
        self, query: str, documents: Sequence[str], top_k: int | None = None
    ) -> list[tuple[int, float]]: ...


class NullReranker:
    """未启用重排。按原顺序返回，分数沿用位置（仅占位）。"""

    enabled = False
    model = "none"

    def rerank(
        self, query: str, documents: Sequence[str], top_k: int | None = None
    ) -> list[tuple[int, float]]:
        limit = len(documents) if top_k is None else min(top_k, len(documents))
        return [(index, 0.0) for index in range(limit)]


class OpenAICompatibleReranker:
    """OpenAI / Cohere 风格的 /rerank 接口（硅基流动、Jina、Cohere 均兼容）。"""

    enabled = True

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise RerankError("缺少 RERANK_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._timeout = timeout

    def rerank(
        self, query: str, documents: Sequence[str], top_k: int | None = None
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        payload: dict[str, object] = {
            "model": self.model,
            "query": query,
            "documents": list(documents),
        }
        if top_k is not None:
            payload["top_n"] = int(top_k)

        try:
            response = net.shared_client().post(
                f"{self.base_url}/rerank",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # 同 embedding：把传输层失败归一到自己的异常类型，
            # 否则 REST 层只能把它当成未知错误报成 500
            raise RerankError(f"连不上重排接口 {self.base_url}：{exc}") from exc
        if response.status_code >= 400:
            raise RerankError(f"重排接口返回 {response.status_code}：{response.text[:300]}")

        body = response.json()
        items = body.get("results") or body.get("data") or []
        out: list[tuple[int, float]] = []
        for item in items:
            index = item.get("index")
            score = item.get("relevance_score", item.get("score", 0.0))
            if index is None:
                continue
            out.append((int(index), float(score)))
        out.sort(key=lambda pair: pair[1], reverse=True)
        if top_k is not None:
            out = out[:top_k]
        return out


class OverlapReranker:
    """确定性离线重排：词项重叠度打分。

    用途同 HashingEmbedding——让重排分支在离线环境下也能被测试。
    """

    enabled = True
    model = "offline-overlap"

    def __init__(self, segmenter: Segmenter | None = None) -> None:
        self._segmenter = segmenter

    @property
    def segmenter(self) -> Segmenter:
        if self._segmenter is None:
            self._segmenter = get_segmenter()
        return self._segmenter

    def rerank(
        self, query: str, documents: Sequence[str], top_k: int | None = None
    ) -> list[tuple[int, float]]:
        query_tokens = set(self.segmenter.tokens(query))
        scored: list[tuple[int, float]] = []
        for index, document in enumerate(documents):
            doc_tokens = set(self.segmenter.tokens(document))
            if not query_tokens or not doc_tokens:
                scored.append((index, 0.0))
                continue
            overlap = len(query_tokens & doc_tokens)
            # 归一化到 0-1，便于与真模型的分数量级接近
            score = overlap / (len(query_tokens) ** 0.5 * len(doc_tokens) ** 0.5)
            scored.append((index, round(score, 6)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        if top_k is not None:
            scored = scored[:top_k]
        return scored


def build_reranker(
    enabled: bool,
    api_key: str,
    base_url: str,
    model: str,
    segmenter: Segmenter | None = None,
) -> Reranker:
    if not enabled:
        return NullReranker()
    if not api_key:
        # 显式启用了重排却没有 Key：退到离线实现并把事实摆在返回值里
        return OverlapReranker(segmenter)
    return OpenAICompatibleReranker(base_url, api_key, model)


__all__ = [
    "NullReranker",
    "OpenAICompatibleReranker",
    "OverlapReranker",
    "RerankError",
    "Reranker",
    "build_reranker",
]
