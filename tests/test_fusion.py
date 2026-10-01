"""RRF 融合测试。

其中 `test_arm_ranks_agrees_with_rrf` 对应一个实测到的 bug：
`arm_ranks` 曾写成 `enumerate(ordered, start=1)` 再 `index + 1`，排名从 2 开始，
于是调试面板显示的排名与实际融合用的排名不一致——面板会给出误导性的信息。
"""

from __future__ import annotations

import pytest

from duramem.retrieval.fusion import arm_ranks, ranked_ids, rrf_fuse


def test_rrf_prefers_items_found_by_both_arms():
    """两路都命中的应胜过只在单路排第一的——这正是 RRF 相对单路的价值。"""
    rankings = {"vector": [1, 2, 3], "lexical": [4, 2, 5]}
    scores = rrf_fuse(rankings, k=60)
    assert ranked_ids(scores)[0] == 2


def test_rrf_k_controls_rank_sensitivity():
    rankings = {"vector": [1, 2], "lexical": [2]}
    small_k = rrf_fuse(rankings, k=1)
    large_k = rrf_fuse(rankings, k=1000)
    # k 越小，排名差异对分数的放大越明显
    assert abs(small_k[1] - small_k[2]) > abs(large_k[1] - large_k[2])


def test_rrf_ignores_duplicate_positions():
    """同一个 id 在同一路里出现两次，只按最优排名计分。"""
    scores = rrf_fuse({"vector": [7, 7, 7]}, k=60)
    assert scores[7] == pytest.approx(1 / 61)


def test_arm_ranks_agrees_with_rrf():
    """排名口径必须与融合口径一致，否则调试面板会显示与实际不符的排名。"""
    ordered = [9, 3, 5]
    ranks = arm_ranks(ordered)
    assert ranks[9] == 1
    assert ranks[3] == 2
    assert ranks[5] == 3

    scores = rrf_fuse({"vector": ordered}, k=60)
    # 融合分数反推的排名必须与 arm_ranks 一致
    for item, rank in ranks.items():
        assert scores[item] == pytest.approx(1 / (60 + rank))


def test_weight_zero_disables_arm():
    scores = rrf_fuse({"vector": [1], "lexical": [2]}, weights={"lexical": 0})
    assert 1 in scores
    assert 2 not in scores


def test_rrf_rejects_bad_k():
    with pytest.raises(ValueError):
        rrf_fuse({"vector": [1]}, k=0)


def test_ranked_ids_is_stable_on_ties():
    assert ranked_ids({5: 1.0, 2: 1.0, 9: 0.5}) == [2, 5, 9]
