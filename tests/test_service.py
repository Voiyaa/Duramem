"""Service 层测试：多库隔离、跨库开关、uid 寻址、命名权、采集。"""

from __future__ import annotations

import pytest
from conftest import dialogue_messages

from duramem.models import Message
from duramem.service import DuramemError


@pytest.fixture
def two_dbs(seeded):
    seeded.create_database("life")
    return seeded


# ====================================================================== 多库


def test_databases_are_isolated_by_default(two_dbs):
    life = [Message("life-win", "life-chat", i, role, content)
            for i, (role, content) in enumerate([("user", "周末想去爬山"), ("assistant", "好主意")])]
    two_dbs.ingest(life, db="life", auto_summarize=False)
    two_dbs.summarize("life-win", "life-chat", db="life")

    work = two_dbs.search("端口", db="work")
    life_res = two_dbs.search("爬山", db="life")

    assert all(hit.db == "work" for hit in work.hits)
    assert all(hit.db == "life" for hit in life_res.hits)
    assert life_res.hits, "生活库应能检索到自己的内容"

    # 工作库不应看到生活库的内容
    leak = two_dbs.search("爬山", db="work")
    assert all(hit.db == "work" for hit in leak.hits)


def test_search_no_longer_takes_scope(two_dbs):
    """跨库检索已经去掉：库与库是硬隔离，检索口子不该存在。

    保留这条断言，是为了让"哪天又有人想加一个跨库开关"时先撞到这里，
    去看设计文档里的理由（隔离是结构性保证，一个口子会把它降级成默认值）。
    """
    with pytest.raises(TypeError):
        two_dbs.search("端口", scope="all")  # type: ignore[call-arg]


# ====================================================================== 当前记忆库


def test_active_db_pointer_drives_default(two_dbs):
    """"当前记忆库"由前端选定的指针决定，不是"注册表第一条"。"""
    two_dbs.ingest(
        [
            Message("life-win", "life-chat", 0, "user", "周末想去爬山"),
            Message("life-win", "life-chat", 1, "assistant", "可以，记得带水和干粮"),
        ],
        db="life",
        auto_summarize=False,
    )
    two_dbs.summarize("life-win", "life-chat", db="life")

    # 默认（两台库按创建时间，work 在前）
    assert two_dbs.ensure_default_db() == "work"

    two_dbs.set_active_db("life")
    assert two_dbs.ensure_default_db() == "life"
    # 不带 db 的检索因此落到 life，而不是第一条
    res = two_dbs.search("爬山")
    assert res.hits and all(hit.db == "life" for hit in res.hits)


def test_active_db_pointer_is_read_freshly(two_dbs):
    """指针变了必须立刻看到——热切换就靠这个（mtime 缓存不能把它挡住）。"""
    two_dbs.set_active_db("life")
    assert two_dbs.ensure_default_db() == "life"
    two_dbs.set_active_db("work")
    assert two_dbs.ensure_default_db() == "work"


def test_dangling_active_db_pointer_raises_instead_of_falling_back(two_dbs):
    """指针指向一个已不存在的库时**报错**，不静默兜底到别的库。

    静默兜底正是"看起来正常、其实查错库"的来源（实测踩过：`dm_stats`
    报成了另一个库的数字）。
    """
    two_dbs.set_active_db("life")
    # 绕过 registry 删掉文件与登记，模拟"库被外部删了"
    two_dbs.registry.remove("life")

    with pytest.raises(DuramemError) as excinfo:
        two_dbs.ensure_default_db()
    assert "life" in str(excinfo.value)
    assert "重新选定" in str(excinfo.value)


def test_set_active_db_refuses_unknown_name(two_dbs):
    with pytest.raises(DuramemError) as excinfo:
        two_dbs.set_active_db("nope")
    assert "库不存在" in str(excinfo.value)


def test_deleting_active_db_switches_the_pointer(two_dbs):
    """删掉当前记忆库时要顺手改指针——否则模型那边每次调用都撞"库已不存在"。"""
    two_dbs.set_active_db("life")
    payload = two_dbs.delete_database("life", purge_file=True)

    assert payload["active_switched_to"] == "work"
    assert two_dbs.ensure_default_db() == "work"


def test_corrupt_active_pointer_is_treated_as_unset(two_dbs):
    """指针文件坏掉 = 当没选定（回落），但要说出来，不能假装无事。"""
    from duramem import active_db

    active_db.write(two_dbs.settings.data_dir, "life")
    active_db.path_for(two_dbs.settings.data_dir).write_text("{ 坏掉的 json", encoding="utf-8")
    two_dbs._active_cache = None

    info = two_dbs.active_view()
    assert info["db"] is None
    assert info["warning"]
    assert two_dbs.ensure_default_db() == "work", "坏文件不该让服务不可用"


# ====================================================================== uid 寻址


def test_uid_resolves_without_db(two_dbs):
    uid = two_dbs.search("端口", db="work").hits[0].uid
    result = two_dbs.read_original(uid, mode="full")
    assert result.ok, "不传 db 时应能在已挂载的库中定位 uid"


def test_uid_resolves_with_db_hint(two_dbs):
    uid = two_dbs.search("端口", db="work").hits[0].uid
    assert two_dbs.read_original(uid, db="work", mode="full").ok


def test_unknown_uid_returns_graceful_result(two_dbs):
    """工具层不该向模型抛错——回查失败要返回统一形状的结果。"""
    result = two_dbs.read_original("not-a-real-uid")
    assert result.ok is False
    assert result.text == ""
    assert result.warnings
    assert "db" in result.warnings[0], "错误信息应提示跨库时需带 db 字段"


def test_wrong_db_hint_returns_graceful_result(two_dbs):
    uid = two_dbs.search("端口", db="work").hits[0].uid
    result = two_dbs.read_original(uid, db="life")
    assert result.ok is False
    assert any("life" in w or "存在" in w for w in result.warnings)


def test_forget_on_unknown_uid_reports_error(two_dbs):
    """写操作与读操作不同：删除一个不存在的 uid 应当被当作错误。"""
    with pytest.raises(DuramemError):
        two_dbs.forget("not-a-real-uid")


def test_uid_survives_database_rename(two_dbs):
    """命名权：库改名不影响 uid 寻址。"""
    uid = two_dbs.search("端口", db="work").hits[0].uid
    two_dbs.rename_database("work", "工作")

    result = two_dbs.read_original(uid, db="工作", mode="full")
    assert result.ok
    assert result.text


def test_uid_survives_enable_disable_cycle(two_dbs):
    uid = two_dbs.search("端口", db="work").hits[0].uid
    two_dbs.forget(uid, db="work")
    assert not two_dbs.read_original(uid, db="work").ok, "删除后原文应不可回查"
    two_dbs.restore(uid, db="work")
    assert two_dbs.read_original(uid, db="work", mode="full").ok


# ====================================================================== 采集


def test_ingest_reports_accepted_count(service):
    result = service.ingest(dialogue_messages(), db="work", auto_summarize=False)
    assert result["ok"] and result["accepted"] == len(dialogue_messages())
    assert len(result["message_ids"]) == len(dialogue_messages())


def test_ingest_is_idempotent(service):
    service.ingest(dialogue_messages(), db="work", auto_summarize=False)
    service.ingest(dialogue_messages(), db="work", auto_summarize=False)
    stats = service.stats("work")
    assert stats["messages"] == len(dialogue_messages())


def test_auto_summary_off_by_default(service):
    service.settings.auto_summary_enabled = False
    result = service.ingest(dialogue_messages(), db="work")
    assert "auto_summary" not in result
    assert service.stats("work")["chunks_alive"] == 0


def test_auto_summary_triggers_when_enabled(service):
    service.settings.auto_summary_enabled = True
    service.settings.summary_interval = 4
    result = service.ingest(dialogue_messages(), db="work")
    assert result["auto_summary"]["ok"] is True
    assert service.stats("work")["chunks_alive"] > 0


def test_manual_store_and_dedupe(service):
    first = service.store("一条手工写入的记忆", "对应原文", db="work")
    assert first["ok"] and first["duplicate"] is False

    second = service.store("一条手工写入的记忆", "对应原文", db="work")
    assert second["duplicate"] is True
    assert second["uid"] == first["uid"]
    assert service.stats("work")["chunks_alive"] == 1


def test_manual_store_is_searchable(service):
    service.store("用户偏好用 sqlite-vec 而不是 zvec", title="向量库偏好", db="work")
    res = service.search("sqlite-vec", db="work")
    assert res.hits
    assert "sqlite-vec" in res.hits[0].summary_text


def test_store_without_original_is_allowed(service):
    payload = service.store("只有摘要没有原文的记忆", db="work")
    chunk = service._components("work").repo.get_chunk(payload["uid"])
    assert chunk["original_text"] == ""
    res = service.search("只有摘要", db="work")
    assert res.hits[0].has_original is False


# ====================================================================== 管理


def test_list_databases_reports_compatibility(two_dbs):
    listing = two_dbs.list_databases()
    names = {item["name"] for item in listing}
    assert names == {"work", "life"}
    for item in listing:
        assert item["compatible"] is True
        assert item["embedding_model"]
        assert item["embedding_dim"] == 1024


def test_stats_shape(seeded):
    stats = seeded.stats("work")
    for field in ("chunks_alive", "messages", "vectors", "size_bytes", "index", "cursors"):
        assert field in stats
    assert stats["index"]["backend"] == "sqlite-vec"
    assert stats["index"]["dim"] == 1024


def test_delete_database_purges_file(two_dbs):
    from pathlib import Path

    path = Path(two_dbs.registry.get("life").file_path)
    assert path.exists()
    two_dbs.delete_database("life", purge_file=True)
    assert not path.exists()
    assert {entry.display_name for entry in two_dbs.registry.list()} == {"work"}


def test_delete_database_reports_file_left_behind(two_dbs, monkeypatch):
    """文件删不掉时必须如实回报，而不是报一句"已删除"。

    被别的进程开着（MCP 子进程 / serve / hook）时 Windows 上删不掉。注册表条目
    这时已经没了，若不说清楚，用户以为删干净了——下次扫描数据目录又把它收养回来，
    看起来像"删了又回来"。所以用打桩模拟占用（不依赖平台差异）。
    """
    from pathlib import Path

    path = Path(two_dbs.registry.resolve_path(two_dbs.registry.get("life")))
    assert path.exists()

    real_unlink = Path.unlink

    def refusing_unlink(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if self == path:
            raise PermissionError("模拟：文件被占用")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)
    result = two_dbs.delete_database("life", purge_file=True)

    assert result["ok"] is True and result["purged"] is True
    assert result["file_removed"] is False
    assert result["leftover"] == [str(path)]
    assert any("收养" in item for item in result["warnings"])
    assert path.exists(), "删不掉就留着，不能假装删了"
    assert {entry.display_name for entry in two_dbs.registry.list()} == {"work"}


def test_delete_database_without_purge_keeps_file(two_dbs):
    from pathlib import Path

    path = Path(two_dbs.registry.resolve_path(two_dbs.registry.get("life")))
    result = two_dbs.delete_database("life", purge_file=False)

    assert result["purged"] is False
    assert result["file_removed"] is False
    assert "leftover" not in result, "没要求删文件就不该有残留告警"
    assert path.exists()


def test_debug_search_payload(seeded):
    payload = seeded.debug_search("ERR_CONN_REFUSED_0x7f", db="work")
    assert payload["db"] == "work"
    assert payload["arms"]["vector"]
    assert payload["arms"]["lexical"]
    assert payload["final"]
    assert payload["params"]["rrf_k"] == seeded.settings.rrf_k
    assert all("arm" in item for item in payload["fusion_pool"])


def test_update_chunk_reembeds(seeded):
    """编辑摘要后必须重算向量——而且要在**向量路**上验证，
    否则词法路会把结果捞回来，掩盖"向量没更新"的问题。

    （vec0 不支持 INSERT OR REPLACE，曾因此静默失败、一直沿用旧向量。）
    """
    uid = seeded.search("端口", db="work").hits[0].uid
    components = seeded._components("work")
    before = components.index.count()

    seeded.update_chunk(uid, summary_text="改成了一段全新的摘要内容，包含独特词 ZZZUNIQUE", db="work")
    assert components.index.count() == before, "重嵌入应替换而非新增行"

    # 向量路单独验证：不要关掉词法路，否则测不出向量是否真的更新了
    res = components.pipeline.search("ZZZUNIQUE")
    assert res.hits, "改过的摘要应能被检索到"
    assert res.hits[0].uid == uid
    assert res.hits[0].vec_rank is not None, "应命中向量路，说明向量已重算"


def test_update_chunk_vector_is_fresh_not_stale(seeded):
    """换掉内容后，旧内容不应再把这条切片推到前面（说明向量真的换了）。"""
    uid = seeded.search("端口", db="work").hits[0].uid
    seeded.update_chunk(uid, summary_text="完全无关的新内容，只讲烘焙面包的发酵温度", db="work")

    components = seeded._components("work")
    res = components.pipeline.search("烘焙面包发酵")
    assert res.hits and res.hits[0].uid == uid

    stale = components.pipeline.search("BACKEND_PORT 端口占用")
    assert not stale.hits or stale.hits[0].uid != uid, "旧向量若未被替换，这里会把已改过的切片捞回来"

def test_renaming_active_db_moves_the_pointer(two_dbs):
    """指针存的是显示名，所以改名必须同步指针。

    实测踩到的就是这条：把当前库改名后指针留在旧名字上，之后所有不带 db 的调用
    （导入、CLI、界面那条路径）全部报"当前记忆库 X 已不存在"。
    """
    two_dbs.set_active_db("work")
    payload = two_dbs.rename_database("work", "工作")

    assert payload["active_renamed_to"] == "工作"
    assert two_dbs.ensure_default_db() == "工作"


def test_renaming_other_db_leaves_pointer_alone(two_dbs):
    two_dbs.set_active_db("work")
    payload = two_dbs.rename_database("life", "生活")

    assert "active_renamed_to" not in payload
    assert two_dbs.ensure_default_db() == "work"


def test_purge_chunk_removes_row_vector_and_fts(seeded):
    """真删除：行、向量、FTS 索引一起清掉，且原文不动。"""

    repo = seeded._components("work").repo
    hit = seeded.search("端口", db="work").hits[0]
    index = seeded._components("work").index
    before_chunks = repo.count_chunks(alive_only=False)
    before_vectors = index.count()
    assert before_vectors >= 1

    # 先做一次软删除，确认真删除对"已软删"的切片也有效
    seeded.forget(hit.uid, db="work")
    payload = seeded.purge_chunk(hit.uid, db="work")

    assert payload["ok"] is True and payload["was_deleted"] is True
    assert payload["vector_removed"] is True
    assert repo.get_chunk(hit.uid, include_deleted=True) is None
    assert repo.count_chunks(alive_only=False) == before_chunks - 1
    assert index.count() == before_vectors - 1, "向量也要一起删掉"
    # FTS 是 external-content 表，靠触发器同步——行没了索引就该没
    left = repo.db.read_conn.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", ("端口",)
    ).fetchone()[0]
    assert left == 0


def test_purge_chunk_unlinks_version_chain(seeded):
    """别的切片把这个 uid 记成继任者时，真删除要清掉那条悬空引用。"""
    from duramem.models import ChunkDraft

    repo = seeded._components("work").repo
    old = repo.insert_chunk(ChunkDraft(summary_text="旧版本记忆", original_text="旧"))["uid"]
    new = repo.insert_chunk(ChunkDraft(summary_text="新版本记忆", original_text="新"))["uid"]
    repo.supersede(old, new)

    payload = seeded.purge_chunk(new, db="work")
    assert payload["ok"] is True
    assert payload["unlinked_superseded"] == 1
    after = repo.get_chunk(old, include_deleted=True)
    assert after["superseded_by"] is None, "不能留下指向已删切片的悬空指针"


def test_purge_chunk_refuses_unknown_uid(seeded):
    with pytest.raises(DuramemError):
        seeded.purge_chunk("不存在", db="work")
