"""RRF（Reciprocal Rank Fusion）融合。

设计文档 v2 的核心变更之一：两路检索**并行融合**，不存在"向量失败才启用词法"
的判据。RRF 只使用排名，天然免疫两路分值的量纲差异——
向量距离是余弦/欧氏距离，BM25 是负对数似然量级，直接加权求和既脆弱又要反复调参。

    score(d) = Σ_arms  1 / (k + rank_arm(d))     rank 从 1 开始
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def rrf_fuse(
    rankings: Mapping[str, Sequence[int]],
    k: int = 60,
    weights: Mapping[str, float] | None = None,
) -> dict[int, float]:
    """把多路有序候选融合成一个 {item: score} 字典。

    `rankings` 的值必须按相关性从优到劣排列；重复 id 只记首次出现的排名。
    """
    if k <= 0:
        raise ValueError("RRF k 必须为正数")

    scores: dict[int, float] = {}
    for arm, ordered in rankings.items():
        arm_weight = 1.0 if weights is None else float(weights.get(arm, 1.0))
        if arm_weight <= 0:
            continue
        seen: set[int] = set()
        for position, item in enumerate(ordered, start=1):
            item = int(item)
            if item in seen:
                continue
            seen.add(item)
            scores[item] = scores.get(item, 0.0) + arm_weight / (k + position)
    return scores


def ranked_ids(scores: Mapping[int, float]) -> list[int]:
    """按分数降序、id 升序（保证同分时结果稳定）排列。"""
    return sorted(scores, key=lambda item: (-scores[item], item))


def arm_ranks(ordered: Sequence[int]) -> dict[int, int]:
    """把有序列表转成 {id: 1-based rank}。

    必须与 `rrf_fuse` 的排名口径完全一致——否则调试面板会显示与实际融合
    不符的排名，面板就成了误导用户的假信息。此处以 `rrf_fuse` 为准：
    最优项排名为 1。
    """
    return {int(item): index + 1 for index, item in enumerate(ordered)}


__all__ = ["arm_ranks", "ranked_ids", "rrf_fuse"]
