"""REST 接口测试。

前端依赖这些接口的形状与上限，所以契约要锁住。这一批的直接起因是一个真实 bug：
时间线页请求 `limit=2000` 而接口上限是 500，FastAPI 返回 422，
而 422 的错误体是**数组**，前端一旦格式化不当就只显示 "[object Object]"，
真实的集成问题被错误信息藏了起来。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from duramem.api import create_app


@pytest.fixture
def client(seeded) -> TestClient:
    return TestClient(create_app(service=seeded, settings=seeded.settings))


# ====================================================================== 基础


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert "work" in body["databases"]
    assert body["offline_embedding"] is True


def test_settings_view_shape(client):
    body = client.get("/api/settings").json()
    assert body["tunable"]
    assert body["readonly"]
    assert body["needs_restart"]
    # 密钥必须打码，不能因为"本地服务"就把 Key 明文吐给前端
    assert body["readonly"]["embedding_api_key"] in {"", "***"}


def test_settings_patch_applies_and_rejects(client):
    ok = client.patch("/api/settings", json={"rrf_k": 42}).json()
    assert ok["ok"] is True and ok["applied"] == {"rrf_k": 42}

    bad = client.patch("/api/settings", json={"embedding_api_key": "x", "vector_top_k": -1}).json()
    assert bad["ok"] is False
    assert "embedding_api_key" in bad["rejected"] and "vector_top_k" in bad["rejected"]


def test_settings_reset(client):
    client.patch("/api/settings", json={"rrf_k": 42})
    client.post("/api/settings/reset", json={"keys": ["rrf_k"]})
    by_name = {item["name"]: item for item in client.get("/api/settings").json()["tunable"]}
    assert by_name["rrf_k"]["value"] == 60
    assert by_name["rrf_k"]["overridden"] is False


# ====================================================================== 库


def test_databases_list_exposes_frontend_fields(client):
    item = client.get("/api/databases").json()["databases"][0]
    for key in (
        "name",
        "file_path",
        "db_uuid",
        "chunks_alive",
        "chunks_missing_vectors",
        "messages",
        "vectors",
        "size_bytes",
        "wal_bytes",
        "embedding_dim",
        "compatible",
        "cold_start",
    ):
        assert key in item, f"前端依赖 {key}"
    assert isinstance(item["embedding_dim"], int), "db_meta 是文本表，接口必须转回 int"
    assert item["compatible"] is True


def test_cold_start_is_per_database(client):
    """每库的冷启动配置：默认空且开；PATCH 可整体/部分更新；写进 db_meta 后列表同步。"""
    body = client.get("/api/databases/work/cold-start").json()
    assert body == {"db": "work", "note": "", "enabled": True}

    ok = client.patch("/api/databases/work/cold-start", json={"note": "项目代号 K3"}).json()
    assert ok == {"db": "work", "note": "项目代号 K3", "enabled": True}
    # 部分更新：只动开关，note 保持
    ok = client.patch("/api/databases/work/cold-start", json={"enabled": False}).json()
    assert ok == {"db": "work", "note": "项目代号 K3", "enabled": False}
    # 清空留言 = 该库关闭
    ok = client.patch("/api/databases/work/cold-start", json={"note": ""}).json()
    assert ok["note"] == ""

    # 配置存在库自己的 db_meta 里，库列表直接带回（前端行内展示用）
    rows = client.get("/api/databases").json()["databases"]
    work = next(item for item in rows if item["name"] == "work")
    assert work["cold_start"] == {"note": "", "enabled": False}

    # 两个库互不影响
    client.patch("/api/databases/work/cold-start", json={"note": "work 的", "enabled": True})
    client.post("/api/databases", json={"name": "life_cs"})
    other = client.get("/api/databases/life_cs/cold-start").json()
    assert other == {"db": "life_cs", "note": "", "enabled": True}

    assert client.patch("/api/databases/nope/cold-start", json={"note": "x"}).status_code == 404
    assert client.get("/api/databases/nope/cold-start").status_code == 404


def test_create_and_rename_database(client):
    assert client.post("/api/databases", json={"name": "life2"}).status_code == 200
    assert client.post("/api/databases", json={"name": "life2"}).status_code == 409

    renamed = client.patch("/api/databases/life2", json={"new_name": "生活"}).json()
    assert renamed["name"] == "生活"
    assert client.get("/api/databases/生活/stats").status_code == 200
    assert client.get("/api/databases/life2/stats").status_code == 404


def test_unknown_database_is_404_not_500(client):
    assert client.get("/api/databases/nope/stats").status_code == 404


def test_mcp_last_call_endpoint(client, seeded):
    """调试页的数据源：MCP 子进程没写过时 exists=False，写了就原样带回。"""
    assert client.get("/api/mcp/last-call").json() == {"exists": False}

    from duramem.mcp_server import _LastCallRecorder

    recorder = _LastCallRecorder(seeded.settings.data_dir / "mcp_last_call.json")
    recorder.record("dm_search", {"query": "q"}, {"results": []}, "work", 7)

    body = client.get("/api/mcp/last-call").json()
    assert body["exists"] is True
    assert body["tool"] == "dm_search"
    assert body["duration_ms"] == 7


def test_stats_includes_model_brief(client):
    """stats_brief 是前端状态卡与模型 dm_stats 共用的同一份投影。

    REST 全量响应里带 brief；brief 只留模型用得上的字段，管理页专用的
    （索引后端 / 文件大小 / 逐会话游标 / 重排模型）不进投影。
    """
    full = client.get("/api/databases/work/stats").json()
    brief = full["brief"]
    for key in ("db", "active_db", "chunks_alive", "vectors", "messages", "sessions_summarized"):
        assert key in brief, f"brief 缺 {key}"
    for admin in ("index", "cursors", "size_bytes", "wal_bytes", "rerank_model",
                  "summary_truncated", "chunks_deleted"):
        assert admin not in brief, f"{admin} 不该进模型的上下文"


def test_stats_brief_conditional_fields(seeded):
    """异常信号是条件字段：出现即值得注意，不出现不占一个字节。"""
    stats = {
        "db": "work", "chunks_alive": 10, "vectors": 10, "messages": 5,
        "cursors": [{"updated_at": "2026-09-29T00:00:00+00:00"}],
        "summary_truncated": 8, "vectors_stale": True, "chunks_missing_vectors": 3,
    }
    brief = seeded.stats_brief(stats, {"db": "work"})
    assert brief["sessions_summarized"] == 1
    assert brief["latest_summary_at"] == "2026-09-29T00:00:00+00:00"
    assert brief["vectors_stale"] is True
    assert brief["chunks_missing_vectors"] == 3
    assert brief["summary_truncated_ratio"] == 0.8  # 8/10 超过 20% 告警线

    clean = seeded.stats_brief(
        {"db": "work", "chunks_alive": 10, "vectors": 10, "messages": 5, "cursors": []},
        {"db": "work"},
    )
    for field in ("vectors_stale", "chunks_missing_vectors", "summary_truncated_ratio",
                  "warnings", "latest_summary_at"):
        assert field not in clean


# ====================================================================== 切片


def test_chunks_total_matches_the_same_filters(client, seeded):
    """total 必须与列表用同一套过滤条件，否则界面上会出现
    "共 11 条…显示前 10"这种自相矛盾的信息，看起来像结果被截断了。"""
    # 先造一条被取代的切片，否则"含被取代的"与"不含"没有区别，断言就是空的
    from duramem.models import ChunkDraft

    repo = seeded._components("work").repo
    old = repo.insert_chunk(ChunkDraft(summary_text="会被取代的旧记忆"))
    repo.insert_chunk(ChunkDraft(summary_text="取代它的新记忆"), supersede_uid=old["uid"])

    plain = client.get("/api/chunks", params={"db": "work"}).json()
    assert plain["total"] == len(plain["chunks"])

    with_deleted = client.get(
        "/api/chunks", params={"db": "work", "include_deleted": True}
    ).json()
    assert with_deleted["total"] == len(with_deleted["chunks"])

    everything = client.get(
        "/api/chunks",
        params={"db": "work", "include_deleted": True, "include_superseded": True},
    ).json()
    assert everything["total"] == len(everything["chunks"])
    assert everything["total"] > with_deleted["total"], "含被取代的应更多"


def test_chunks_limit_contract(client):
    """时间线页要一次拉全部切片做聚合。上限一旦低于它会直接 422，
    而 422 的错误体是数组——前端格式化不当就只剩 [object Object]。"""
    response = client.get("/api/chunks", params={"db": "work", "limit": 2000})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "chunks" in body and "total" in body

    # 超过上限要给出可读的 422（detail 是数组，不是字符串）
    over = client.get("/api/chunks", params={"db": "work", "limit": 99_999})
    assert over.status_code == 422
    detail = over.json()["detail"]
    assert isinstance(detail, list) and detail, "前端必须能识别这种形状并转成人话"


def test_chunk_detail_includes_neighbours(client):
    listing = client.get("/api/chunks", params={"db": "work"}).json()["chunks"]
    uid = listing[0]["chunk_uid"]
    body = client.get(f"/api/chunks/{uid}", params={"db": "work"}).json()
    assert body["chunk_uid"] == uid
    assert "neighbours" in body and isinstance(body["neighbours"], list)
    assert body["original_text"] != "", "默认应带上原文供详情页展示"


def test_chunk_detail_can_omit_original(client):
    uid = client.get("/api/chunks", params={"db": "work"}).json()["chunks"][0]["chunk_uid"]
    body = client.get(
        f"/api/chunks/{uid}", params={"db": "work", "with_original": False}
    ).json()
    assert body["original_text"] == ""


def test_patch_chunk_and_uid_stability(client):
    uid = client.get("/api/chunks", params={"db": "work"}).json()["chunks"][0]["chunk_uid"]
    result = client.patch(
        f"/api/chunks/{uid}", params={"db": "work"}, json={"title": "前端改的标题"}
    ).json()
    assert result["ok"] is True
    assert "title" in result["applied"]

    after = client.get(f"/api/chunks/{uid}", params={"db": "work"}).json()
    assert after["title"] == "前端改的标题"
    assert after["chunk_uid"] == uid, "改名不改句柄"


def test_patch_empty_body_is_400(client):
    uid = client.get("/api/chunks", params={"db": "work"}).json()["chunks"][0]["chunk_uid"]
    assert client.patch(f"/api/chunks/{uid}", params={"db": "work"}, json={}).status_code == 400


def test_delete_and_restore_chunk(client):
    uid = client.get("/api/chunks", params={"db": "work"}).json()["chunks"][0]["chunk_uid"]
    assert client.delete(f"/api/chunks/{uid}", params={"db": "work"}).json()["ok"] is True

    alive = client.get("/api/chunks", params={"db": "work"}).json()
    assert uid not in [item["chunk_uid"] for item in alive["chunks"]]
    with_deleted = client.get(
        "/api/chunks", params={"db": "work", "include_deleted": True}
    ).json()
    assert uid in [item["chunk_uid"] for item in with_deleted["chunks"]]

    assert client.post(f"/api/chunks/{uid}/restore", params={"db": "work"}).json()["ok"] is True


# ====================================================================== 检索与调试


def test_debug_search_exposes_three_arms(client):
    body = client.post(
        "/api/debug/search", json={"query": "ERR_CONN_REFUSED_0x7f", "db": "work"}
    ).json()
    assert body["arms"]["vector"], "要能看到向量路原始排名"
    assert body["arms"]["lexical"], "要能看到词法路原始排名"
    assert body["final"], "也要有最终结果"
    assert body["params"]["rrf_k"] == 60
    assert all("arm" in item for item in body["fusion_pool"]), "面板靠 arm 着色"

    # 三路排名口径必须一致（曾经有过 off-by-one，面板会显示假排名）
    vec_ranks = [item["vec_rank"] for item in body["arms"]["vector"]]
    assert min(vec_ranks) == 1
    assert vec_ranks == list(range(1, len(vec_ranks) + 1))


def test_debug_search_bad_db_is_400(client):
    response = client.post("/api/debug/search", json={"query": "x", "db": "nope"})
    assert response.status_code == 400
    assert "nope" in response.json()["detail"]


def test_search_then_read_original_via_api(client):
    hit = client.post("/api/search", json={"query": "端口", "db": "work"}).json()["results"][0]
    assert hit["has_original"] is True
    assert hit["_suggestion"], "模型需要这个信号才能判断该不该回查原文"


# ====================================================================== 采集与总结


def test_ingest_and_summarize(client):
    messages = [
        {
            "window_id": "api-win",
            "session_id": "api-session",
            "seq": index,
            "role": role,
            "content": content,
        }
        for index, (role, content) in enumerate(
            [("user", f"第 {i} 条测试消息，包含标记 APIMARK_{i}") for i in range(6)]
        )
    ]
    ingested = client.post(
        "/api/ingest", json={"messages": messages, "db": "work"}
    ).json()
    assert ingested["accepted"] == 6

    outcome = client.post(
        "/api/summarize",
        json={"window_id": "api-win", "session_id": "api-session", "db": "work"},
    ).json()
    assert outcome["ok"] is True
    assert outcome["added"] >= 1

    runs = client.get("/api/summary-runs", params={"db": "work"}).json()["runs"]
    assert runs, "总结记录要能在时间线页看到"
    assert "warnings" in runs[0], "前端要解析这个字段"


def test_sessions_and_messages(client):
    sessions = client.get("/api/sessions", params={"db": "work"}).json()["sessions"]
    assert sessions and sessions[0]["messages"] > 0

    target = sessions[0]
    rows = client.get(
        "/api/messages",
        params={
            "window_id": target["window_id"],
            "session_id": target["session_id"],
            "db": "work",
        },
    ).json()["messages"]
    assert rows and all("content" in row for row in rows)


# ====================================================================== 运维


def test_reindex_and_rebuild_vectors(client):
    stats = client.get("/api/databases/work/stats").json()
    assert client.post("/api/reindex", params={"db": "work"}).json()["embedded"] == stats["chunks_alive"]

    rebuilt = client.post("/api/rebuild-vectors", params={"db": "work"}).json()
    assert rebuilt["embedded"] == stats["chunks_alive"]
    assert client.get("/api/databases/work/stats").json()["chunks_missing_vectors"] == 0


def test_snapshot_endpoint(client):
    body = client.post("/api/snapshot", params={"db": "work"}).json()
    assert body["ok"] is True
    from pathlib import Path

    assert Path(body["path"]).exists()
    assert body["size_bytes"] > 0


def test_embed_pending_endpoint(client):
    body = client.post("/api/embed-pending", params={"db": "work"}).json()
    assert "embedded" in body and "remaining" in body


def test_unknown_db_for_writes_is_404(client):
    assert client.post("/api/ingest", json={"messages": [], "db": "nope"}).status_code == 404

# ====================================================================== 归档


def test_export_and_upload_roundtrip(client, tmp_path):
    """导出 → 上传还原：REST 两条入口（下载 / 上传）与 service 语义一致。"""
    import json

    export = client.get("/api/export", params={"db": "work", "with_messages": True})
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    payload = export.json()
    assert payload["format"] == "duramem-export" and payload["format_version"] == 2
    # seeded 只做了切片总结：有游标，没有会话概览（那是显式的另一步）
    assert payload["cursors"]

    # 浏览器路径：归档原文做请求体，目标库名走 query
    upload = client.post(
        "/api/import-archive/upload",
        params={"db": "restored"},
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    assert upload.status_code == 200, upload.text
    body = upload.json()
    assert body["ok"] and body["db"] == "restored" and body["chunks"] == len(payload["chunks"])
    assert body["warnings"] == []

    # 还原库立即可用：stats 与检索都走同一条 service 路径
    stats = client.get("/api/databases/restored/stats").json()
    assert stats["chunks_alive"] >= 1
    assert stats["chunks_missing_vectors"] == 0


def test_import_archive_rejects_bad_body_and_existing_name(client, tmp_path):
    client.get("/api/export", params={"db": "work"})

    garbage = client.post("/api/import-archive/upload", content=b"not json")
    assert garbage.status_code == 400
    assert "JSON" in garbage.json()["detail"]

    wrong = client.post("/api/import-archive/upload", content=b'{"format": "other"}')
    assert wrong.status_code == 400
    assert "不是 Duramem 导出归档" in wrong.json()["detail"]

    path_req = client.post(
        "/api/import-archive",
        json={"path": str(tmp_path / "missing.json")},
    )
    assert path_req.status_code == 400 and "不存在" in path_req.json()["detail"]
