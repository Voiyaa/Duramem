"""P1 验证脚本 v2：复跑 P0 的 8 道单程失败题 + 图可达性静态分析 + 扩散耗时。

用法：
  .venv/Scripts/python.exe eval/p0/p1_verify.py            # auto 触发
  EXPAND_ALWAYS=1 .venv/Scripts/python.exe eval/p0/p1_verify.py  # 强制扩散
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from duramem.config import get_settings  # noqa: E402
from duramem.service import Service  # noqa: E402
from duramem.store.links import fetch_link_rows  # noqa: E402

QUESTIONS = {
    "Q03": ("在 Duramem 里改配置，为什么有的立即生效、有的要重启？跨进程热重载是怎么实现的、生效范围到哪？",
            {"zICnPezrcAG5", "g9X485G2WSGz", "DqEZL26BO7N5"}),
    "Q04": ("删除能力有几个层级？模型、前端/CLI 各自能删到什么程度？库级真删除怎么防误删？",
            {"FGIrzzWSuskR", "QUPjXo1VOdHp", "7IPnkGifDngm"}),
    "Q06": ("项目测试数量从后端首次完成到现在，经过了哪些关键数字节点？最终是多少？",
            {"jMTpLlaYMiez", "zICnPezrcAG5", "QUPjXo1VOdHp", "wbXfFM5f5S4v", "VYbb6sYaDAfa"}),
    "Q07": ("三级记忆改造前，助手最初建议不照抄 OpenViking，给出的理由是什么？用户为什么最终仍决定上三级？落地后为什么又做了命名重构？",
            {"FYQuxZQmQy2S", "Du2ejrsnWOE8", "VYbb6sYaDAfa"}),
    "Q08": ("window=2 回查被预算拒绝的那次硬失败，具体数字和根因是什么？此前还有过一次同族失败，是什么？这类问题最终怎么解决？",
            {"kRsmNuOxLXzH", "K6LmrVMqBFhV", "wbXfFM5f5S4v"}),
    "Q09": ("词法路（FTS5）的 search_text 由什么组成？这个组成导致过什么检索失败案例？当初为什么坚持词法路每轮都跑？",
            {"g9X485G2WSGz", "y0C08CiWqU4c", "7IPnkGifDngm"}),
    "Q12": ("曾经有人提议让模型直接回传对话原文入库（dm_ingest），为什么被否决？这和系统的哪条核心原则一致？",
            {"6sz0SL88jByJ", "ap9Lbdc0yAFR"}),
    "Q15": ("start.bat / stop.bat 一键脚本踩过哪些坑？stop.bat 为什么只能按端口杀进程、按进程名杀会出什么事？",
            {"kRsmNuOxLXzH", "omtNRKy9XxKM"}),
}

UID_TO_ID: dict[str, int] = {}


def bfs_distance(conn, sources: set[int], targets: set[int], max_depth: int = 2) -> dict[int, int]:
    """多源 BFS：sources 到每个 target 的最短跳数；>max_depth 或不通 → -1。"""
    dist = {s: 0 for s in sources}
    queue = deque(sources)
    while queue:
        node = queue.popleft()
        if dist[node] >= max_depth:
            continue
        rows = fetch_link_rows(conn, [node])
        neighbors = set()
        for row in rows:
            for side in ("src_id", "dst_id"):
                other = int(row[side])
                if other not in dist:
                    neighbors.add(other)
        for neighbor in neighbors:
            dist[neighbor] = dist[node] + 1
            queue.append(neighbor)
    return {t: dist.get(t, -1) for t in targets}


def main() -> int:
    settings = get_settings()
    settings.data_dir = Path(r"E:\work\Duramem\data")
    service = Service(settings)
    comp = service._components("Duramem开发")
    conn = comp.repo.db.read_conn

    # uid → 内部 id 映射（图分析用）
    for row in conn.execute("SELECT id, chunk_uid FROM chunks").fetchall():
        UID_TO_ID[str(row["chunk_uid"])] = int(row["id"])

    mode = "always" if os.environ.get("EXPAND_ALWAYS") else "auto"
    print(f"=== 触发模式：{mode} ===\n")
    print(f"{'题':4} 直接  +扩散  触发原因        扩散明细（关系/跳数）            耗时ms  缺失证据可达性")
    total_gain = 0
    for qid, (query, evidence) in QUESTIONS.items():
        t0 = time.perf_counter()
        result = service.search(query, db="Duramem开发", top_k=5, collect_debug=True)
        elapsed = (time.perf_counter() - t0) * 1000

        direct = {h.uid for h in result.hits}
        expanded = {h.uid for h in result.expanded}
        combined = direct | expanded
        per_db = (result.debug or {}).get("per_db", {}).get("Duramem开发", {})
        exp_dbg = per_db.get("expand", {})
        reason = exp_dbg.get("reason") or "-"
        detail = ",".join(
            f"{item['uid'][:8]}:{item['relation']}/{item['hops']}"
            for item in exp_dbg.get("emitted", [])
        ) or "—"

        missing = evidence - combined
        seed_ids = {UID_TO_ID[u] for u in direct if u in UID_TO_ID}
        missing_ids = {UID_TO_ID[u] for u in missing if u in UID_TO_ID}
        dists = bfs_distance(conn, seed_ids, missing_ids) if missing_ids else {}
        reach = ",".join(
            f"{u[:8]}:{' unreachable' if d < 0 else f'@{d}跳'}"
            for u, d in zip(sorted(missing), (dists[i] for i in [UID_TO_ID[u] for u in sorted(missing)]))
        ) or "—"

        gain = len(combined & evidence) - len(direct & evidence)
        total_gain += max(0, gain)
        print(
            f"{qid:4} {len(direct & evidence)}/{len(evidence)}   "
            f"{len(combined & evidence)}/{len(evidence)}    "
            f"{reason:16} {detail:30} {elapsed:5.0f}   {reach}"
        )

    print(f"\n8 题合计证据增益：+{total_gain}")

    # ---- 扩散本身耗时（不含嵌入/重排）：固定种子重复 200 次
    from duramem.retrieval.expand import expand as graph_expand

    seed_weights = {UID_TO_ID[u]: 1.0 for u in ("FYQuxZQmQy2S", "Du2ejrsnWOE8", "wbXfFM5f5S4v")}
    times = []
    for _ in range(200):
        t0 = time.perf_counter()
        graph_expand(conn, seed_weights, 3)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    print(f"扩散单步耗时（200 次，31 节点图）：P50={times[len(times)//2]:.2f}ms "
          f"P95={times[int(len(times)*0.95)-1]:.2f}ms max={times[-1]:.2f}ms（验收线 50ms）")

    service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
