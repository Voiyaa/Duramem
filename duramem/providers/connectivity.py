"""模型端点连通性探测：拿草稿配置去试一把，不落库。

从 service.py 拆出（设计文档 A.15）：三个 probe 只依赖草稿 `Settings` 与
providers/ 自己的实现，与服务状态无关——它们本来就该住在被探测对象的旁边。
`Service.test_provider_config` 负责草稿构建、错误包装与分组分发。
"""

from __future__ import annotations

import time
from typing import Any

from duramem.config import Settings
from duramem.providers.embedding import build_embedding_provider
from duramem.providers.rerank import OpenAICompatibleReranker
from duramem.text.tokenize import Segmenter


def probe_embedding(draft: Settings, segmenter: Segmenter, started: float) -> dict[str, Any]:
    provider = build_embedding_provider(
        api_key=draft.embedding_api_key,
        base_url=draft.embedding_base_url,
        model=draft.embedding_model,
        dim=draft.embedding_dim,
        batch_size=draft.embed_batch_size,
        force_offline=draft.embedding_fake,
        segmenter=segmenter,
        send_dimensions=draft.embedding_send_dimensions,
    )
    sample = ["Duramem 记忆系统连通性测试", "connectivity probe"]
    vectors = provider.embed(sample)

    if provider.model.startswith("offline-hashing"):
        return {
            "ok": True,
            "group": "embedding",
            "provider": provider.model,
            "offline": True,
            "ms": int((time.perf_counter() - started) * 1000),
            "note": "没有配置 API Key（或勾了强制离线），实际会用确定性的哈希向量。"
            "检索仍然可用，但只有词项重叠、没有语义泛化。",
        }

    got_dim = len(vectors[0]) if vectors else 0
    # 真实端点返回的维度才是事实。用户填错了维度，这里就是最后一次能拦住的地方：
    # 放过去的话，写库时才报错，而那时已经建好了错误的向量表。
    if got_dim != draft.embedding_dim:
        return {
            "ok": False,
            "group": "embedding",
            "provider": provider.model,
            "detected_dim": got_dim,
            "configured_dim": draft.embedding_dim,
            "ms": int((time.perf_counter() - started) * 1000),
            "error": f"维度不符：配置 {draft.embedding_dim}，接口实际返回 {got_dim}。"
            f"把「向量维度」改成 {got_dim} 再保存。",
        }
    return {
        "ok": True,
        "group": "embedding",
        "provider": provider.model,
        "offline": False,
        "detected_dim": got_dim,
        "configured_dim": draft.embedding_dim,
        "batch_size": draft.embed_batch_size,
        "ms": int((time.perf_counter() - started) * 1000),
        "note": f"返回 {got_dim} 维，与配置一致。共 {len(vectors)} 条样本。",
    }


def probe_rerank(draft: Settings, started: float) -> dict[str, Any]:
    if not draft.rerank_api_key:
        return {
            "ok": True,
            "group": "rerank",
            "provider": "offline-overlap",
            "offline": True,
            "ms": int((time.perf_counter() - started) * 1000),
            "note": "没有 Key，启用后会退到离线的词项重叠度重排（可用但弱）。",
        }
    # 即使开关没打开也测——"先验证再启用"才是正常的操作顺序
    reranker = OpenAICompatibleReranker(
        draft.rerank_base_url, draft.rerank_api_key, draft.rerank_model
    )
    documents = [
        "向量检索用 sqlite-vec 做 KNN，BM25 只作为兜底",
        "今天中午吃的是番茄炒蛋和米饭",
    ]
    ranking = reranker.rerank("记忆检索用什么做向量索引", documents, top_k=2)
    where = {index: score for index, score in ranking}
    correct_first = bool(ranking and ranking[0][0] == 0)
    return {
        "ok": True,
        "group": "rerank",
        "provider": draft.rerank_model,
        "offline": False,
        "ms": int((time.perf_counter() - started) * 1000),
        "scores": [round(where.get(i, 0.0), 4) for i in range(len(documents))],
        "note": (
            "相关文档排到了第一位，接口可用。"
            if correct_first
            else "接口通了，但相关文档没有排在第一位——请确认模型名选对了。"
        ),
        "ranking_sane": correct_first,
    }


def probe_summary(draft: Settings, started: float) -> dict[str, Any]:
    from duramem.summarizer import probe_chat

    if not draft.summary_api_key or not draft.summary_base_url:
        return {
            "ok": True,
            "group": "summary",
            "provider": "offline-heuristic",
            "offline": True,
            "ms": int((time.perf_counter() - started) * 1000),
            "note": "地址或 Key 没配全，摘要会走离线的抽取式实现（不调模型）。",
        }
    reply = probe_chat(
        draft.summary_base_url, draft.summary_api_key, draft.summary_model
    )
    return {
        "ok": True,
        "group": "summary",
        "provider": draft.summary_model,
        "offline": False,
        "reply": reply,
        "ms": int((time.perf_counter() - started) * 1000),
        "note": f"模型回了：{reply}",
    }
