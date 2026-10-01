"""图扩散激活（Personalized PageRank）：迭代检索环路的"自动联想"引擎。

设计文档 §16.5 的分工：**图负责"可能相关"（剪枝），模型负责"真的相关"
（语义验证）**。人脑的对应物是"联想大部分不经过意识"——自动、并行、毫秒级。
本模块就是那条毫秒级路径：从直接命中的切片（种子）出发，沿 `links` 图做
带重启的随机游走（PPR），把与种子**多跳相连**但查询词够不着的切片带回来。

为什么用 PPR 而不是简单的一跳邻居展开：
- 一跳展开是 PPR 的特例（damping→1、深度=1），表达力差一个量级；
- PPR 的重启项保证种子主导排序——扩散只做"种子周边的放大"，不会把
  图另一头的热门节点顶到前面；
- 幂迭代在 SQLite 取出的局部子图上跑，几十个节点微秒级，P95 ≤ 50ms
  的验收线（§16.10）留有数量级余量。

深度固定 2（模块常数）：邻接链接是链状的，深度 2 覆盖"前后各两片"；
keyword/tag 链接跨会话，深度 2 已能走到"另一场对话里共享关键词的切片，
再从它走到它的邻接"。更深只会把噪声翻倍。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from duramem.store.links import fetch_link_rows

EXPAND_DEPTH = 2
PPR_DAMPING = 0.85
PPR_MAX_ITER = 30
PPR_TOLERANCE = 1e-9


@dataclass
class ExpandCandidate:
    """一个扩散候选：种子周边被图连带出来的切片。"""

    chunk_id: int
    ppr: float                 # 原始 PPR 质量（全部节点质量之和 = 1）
    score: float               # 归一化分（最高者 = 1.0），给 SearchHit.score 用
    seed_id: int               # 主导种子（把该节点带进图的那个种子）
    hops: int                  # 距种子的跳数（1 或 2）
    relation: str              # 路径第一跳的链接关系（adjacent / tag / keyword）
    path: list[int] = field(default_factory=list)  # 种子 → … → 本节点的 chunk_id 链


def expand(
    read_conn,
    seed_weights: dict[int, float],
    top_k: int,
) -> list[ExpandCandidate]:
    """从种子出发做 PPR 扩散，返回至多 top_k 个非种子候选。

    `seed_weights`：种子 chunk_id → 权重（调用方用直接命中分数归一化）。
    权重决定重启分布——0.99 的种子应当比 0.31 的种子带出更多质量。
    """
    if not seed_weights or top_k <= 0:
        return []

    seed_ids = list(seed_weights)
    frontier = list(seed_ids)
    adjacency: dict[int, dict[int, float]] = {}
    # 每个节点怎么被带进图的：第一跳种子、跳数、路径、关系。BFS 保证先近后远。
    provenance: dict[int, tuple[int, int, str, list[int]]] = {}

    total = float(sum(seed_weights.values())) or 1.0
    restart = {cid: seed_weights[cid] / total for cid in seed_ids}

    for _ in range(EXPAND_DEPTH):
        if not frontier:
            break
        rows = fetch_link_rows(read_conn, frontier)
        next_frontier: list[int] = []
        for row in rows:
            src, dst = int(row["src_id"]), int(row["dst_id"])
            weight = float(row["weight"] or 0.0)
            if weight <= 0:
                continue
            adjacency.setdefault(src, {})[dst] = max(
                weight, adjacency.get(src, {}).get(dst, 0.0)
            )
            adjacency.setdefault(dst, {})[src] = max(
                weight, adjacency.get(dst, {}).get(src, 0.0)
            )
            # 无向边的两个方向各自记一次来源（谁先把对面带进图的）
            for a, b in ((src, dst), (dst, src)):
                if b in provenance or b in seed_weights:
                    continue
                if a in provenance:
                    seed_id, hops, relation, path = provenance[a]
                    provenance[b] = (seed_id, hops + 1, relation, [*path, b])
                else:
                    provenance[b] = (a, 1, str(row["relation"]), [a, b])
                next_frontier.append(b)
        frontier = next_frontier

    if not adjacency:
        return []

    # ---- 幂迭代。局部子图很小，收敛廉价。
    prob = {node: 0.0 for node in adjacency}
    prob.update(restart)
    damping = PPR_DAMPING
    for _ in range(PPR_MAX_ITER):
        delta = 0.0
        nxt = {
            node: (1.0 - damping) * restart.get(node, 0.0) for node in adjacency
        }
        for node, neighbors in adjacency.items():
            mass = prob.get(node, 0.0)
            if mass <= 0.0 or not neighbors:
                continue
            share = damping * mass / len(neighbors)
            for neighbor in neighbors:
                if neighbor in nxt:
                    nxt[neighbor] += share
        for node in adjacency:
            delta = max(delta, abs(nxt[node] - prob.get(node, 0.0)))
            prob[node] = nxt[node]
        if delta <= PPR_TOLERANCE:
            break

    candidates = [
        ExpandCandidate(
            chunk_id=node,
            ppr=mass,
            score=0.0,
            seed_id=provenance[node][0],
            hops=provenance[node][1],
            relation=provenance[node][2],
            path=provenance[node][3],
        )
        for node, mass in prob.items()
        if node not in seed_weights and mass > 0.0 and node in provenance
    ]
    candidates.sort(key=lambda c: -c.ppr)
    candidates = candidates[:top_k]
    if not candidates:
        return []
    top = candidates[0].ppr or 1.0
    for candidate in candidates:
        candidate.score = candidate.ppr / top
    return candidates


__all__ = ["ExpandCandidate", "expand", "EXPAND_DEPTH"]
