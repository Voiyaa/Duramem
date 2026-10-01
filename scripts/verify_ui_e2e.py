"""前端两个功能的端到端验证：「一键导入未导入」与「真删除库」。

用法：
    # 先另起一个用新代码的服务（别用正在跑的那个）
    duramem serve --data-dir <data-dir> --port 8021
    # 再跑这个脚本；默认打 8021、测「测试」库，可用环境变量改
    DURAMEM_E2E_PORT=8021 DURAMEM_E2E_DB=测试 python scripts/verify_ui_e2e.py

只碰「测试」库与一个自建的待删库，不碰 zcode.db：
1. 一键导入的作用域数据从哪来（/api/import/list 的 already_imported 标记）
2. 导入进「测试」库：预览 → 真导入 → 幂等重导 → 切片
3. 真删除：新建库 → DELETE purge_file=true → 文件真的没了 → 注册表里也没了
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 用一个**临时起的**服务跑，别打用户正在用的那个：它会新建/删除库、往测试库里导数据。
PORT = os.environ.get("DURAMEM_E2E_PORT", "8021")
BASE = f"http://127.0.0.1:{PORT}"
TEST_DB = os.environ.get("DURAMEM_E2E_DB", "测试")


def call(method: str, path: str, payload: dict | None = None, timeout: float = 180.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    # 路径里可能有中文库名，urlopen 要求请求行是 ASCII
    url = BASE + urllib.parse.quote(path, safe="/?&=")
    req = urllib.request.Request(
        url, data=data, method=method, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    ok = True

    step("① 库列表（应只有 zcode / 测试，无 zcode-2）")
    _, body = call("GET", "/api/databases")
    names = [item["name"] for item in body["databases"]]
    print("  库:", names)
    for item in body["databases"]:
        print(f"    {item['name']:8} 切片={item['chunks_alive']} 消息={item['messages']} 文件={item['file_path']}")
    ok &= "zcode-2" not in names

    step("② 导入清单里「未导入」怎么标（一键导入的作用域就是这些）")
    _, listing = call(
        "POST",
        "/api/import/list",
        {"source": "zcode", "db": TEST_DB, "include_subagents": False, "refresh": True},
    )
    convs = listing["conversations"]
    pending = [c for c in convs if not c.get("already_imported")]
    print(f"  共 {len(convs)} 个对话，其中未导入 {len(pending)} 个")
    for item in sorted(convs, key=lambda c: c["messages"])[:4]:
        flag = "已导入" if item.get("already_imported") else "未导入"
        print(f"    [{flag}] {item['messages']:4} 条 · {item['title'][:32]}")
    if not pending:
        print("  没有可导入的对话，后续步骤跳过")
        return 2
    target = min(
    (c for c in pending if c["messages"] >= 10),
    key=lambda c: c["messages"],
    default=None,
)
    if target is None:
        print("  没有足够长的未导入对话（太短的会被导入器跳过），跳过后续")
        return 2
    print(f"  选消息最少的那个（够长、不会被跳过）：{target['title'][:32]}"
          f"（{target['messages']} 条，{target['session_id'][:20]}）")

    step("③ 先预览（dry_run，不写数据）")
    _, preview = call(
        "POST",
        "/api/import",
        {"source": "zcode", "db": TEST_DB, "sessions": [target["session_id"]], "dry_run": True},
    )
    print("  ", {k: preview[k] for k in ("conversations_found", "conversations_imported", "messages_imported", "skipped_short")})

    step("④ 真导入（不切片，纯原文）")
    _, before = call("GET", f"/api/databases/{TEST_DB}/stats")
    _, report = call(
        "POST",
        "/api/import",
        {"source": "zcode", "db": TEST_DB, "sessions": [target["session_id"]], "summarize": False},
    )
    print("  ", {k: report[k] for k in ("conversations_imported", "messages_imported", "skipped_short")})
    _, after = call("GET", f"/api/databases/{TEST_DB}/stats")
    added = after["messages"] - before["messages"]
    print(f"  消息 {before['messages']} -> {after['messages']}（+{added}）")
    ok &= added > 0

    step("⑤ 同一个对话再导一次（幂等：消息数不该涨）")
    _, again = call(
        "POST",
        "/api/import",
        {"source": "zcode", "db": TEST_DB, "sessions": [target["session_id"]], "summarize": False},
    )
    _, after2 = call("GET", f"/api/databases/{TEST_DB}/stats")
    print(f"  消息 {after['messages']} -> {after2['messages']}（重复导入报告 imported={again['conversations_imported']}）")
    ok &= after2["messages"] == after["messages"]

    step("⑥ 带切片再导一次（走 DeepSeek 摘要 + BGE-M3 嵌入，验证整条链路）")
    _, sliced = call(
        "POST",
        "/api/import",
        {"source": "zcode", "db": TEST_DB, "sessions": [target["session_id"]], "summarize": True},
    )
    print("  切片结果:", sliced.get("summarized"), "个对话，新增", sliced.get("chunks_added"), "条切片")
    if sliced.get("errors"):
        print("  errors:", sliced["errors"])
    _, after3 = call("GET", f"/api/databases/{TEST_DB}/stats")
    print(f"  切片 {after3['chunks_alive']} · 向量 {after3['vectors']} · 缺向量 {after3['chunks_missing_vectors']}")
    if after3["chunks_alive"]:
        ok &= after3["chunks_missing_vectors"] == 0
        _, hits = call("POST", "/api/search", {"query": target["title"][:20], "db": TEST_DB, "top_k": 3})
        print(f"  检索自检: mode={hits.get('retrieval_mode')} 命中={hits.get('count')}")
        for hit in hits.get("results", [])[:2]:
            print(f"    - {hit['score']:.4f} {hit['title'][:34]}")

    step("⑦ 真删除：新建一个待删库，purge 后文件必须真的消失")
    throwaway = f"待删-{int(time.time()) % 10000}"
    status, _ = call("POST", "/api/databases", {"name": throwaway})
    path = next(
        item["file_path"] for item in call("GET", "/api/databases")[1]["databases"] if item["name"] == throwaway
    )
    print(f"  已建库 {throwaway} -> {path}")
    status, result = call("DELETE", f"/api/databases/{throwaway}?purge_file=true")
    print(f"  DELETE 返回 {status}: {result}")
    ok &= status == 200 and result.get("file_removed") is True
    from pathlib import Path

    exists = Path(path).exists()
    print(f"  文件还在吗: {exists}")
    ok &= not exists
    names_after = [item["name"] for item in call("GET", "/api/databases")[1]["databases"]]
    print("  库列表:", names_after)
    ok &= throwaway not in names_after

    step("⑧ 移除（不删文件）应该留着文件")
    throwaway2 = f"待移除-{int(time.time()) % 10000}"
    call("POST", "/api/databases", {"name": throwaway2})
    path2 = next(
        item["file_path"] for item in call("GET", "/api/databases")[1]["databases"] if item["name"] == throwaway2
    )
    _, result2 = call("DELETE", f"/api/databases/{throwaway2}?purge_file=false")
    print(f"  {result2}  文件还在: {Path(path2).exists()}")
    ok &= result2.get("file_removed") is False and Path(path2).exists()
    # 收尾：把这个残留文件也清掉，别在数据目录里留垃圾
    Path(path2).unlink(missing_ok=True)
    print(f"  已清掉残留文件 {path2}")

    print("\n结论:", "全部通过" if ok else "有未通过项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
