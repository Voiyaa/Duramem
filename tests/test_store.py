"""存储层测试：schema、索引同步、去重、软删除、区间渲染、模型一致性。"""

from __future__ import annotations

import pytest
from conftest import dialogue_messages

from duramem.models import ChunkDraft, Message, render_messages_with_offsets
from duramem.store.registry import DatabaseRegistry, slugify
from duramem.store.repository import Repository, content_hash
from duramem.store.schema import EmbeddingMismatchError


def test_fts_trigger_keeps_index_in_sync(service):
    repo = Repository(service._components("work").db, service.segmenter)
    repo.upsert_messages(dialogue_messages())
    draft = ChunkDraft(
        summary_text="关于端口占用的排查记录，错误码 ERR_CONN_REFUSED_0x7f",
        title="端口排查",
        keywords=["BACKEND_PORT"],
    )
    result = repo.insert_chunk(draft)

    rows = repo.db.read_conn.execute("SELECT count(*) AS c FROM chunks_fts").fetchone()
    assert rows["c"] == 1

    # 错误码必须能被词法检索命中——这是整条词法路的立足点
    hit = repo.db.read_conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
        (service.segmenter.segment_query("ERR_CONN_REFUSED_0x7f"),),
    ).fetchall()
    assert [r["rowid"] for r in hit] == [result["id"]]

    # 删除后索引同步清理
    repo.db.execute_write("DELETE FROM chunks WHERE id = ?", (result["id"],))
    left = repo.db.read_conn.execute("SELECT count(*) AS c FROM chunks_fts").fetchone()
    assert left["c"] == 0


def test_message_upsert_is_idempotent_and_updates_on_edit(service):
    repo = Repository(service._components("work").db, service.segmenter)
    messages = dialogue_messages()

    first = repo.upsert_messages(messages)
    second = repo.upsert_messages(messages)
    assert first == second, "同一位置重复投递不应产生副本"

    edited = list(messages)
    edited[0] = Message(messages[0].window_id, messages[0].session_id, 0, "user", "改过的内容")
    third = repo.upsert_messages(edited)
    assert third[0] == first[0], "同一 seq 应复用原 id"
    stored = repo.get_messages([first[0]])[0]
    assert stored["content"] == "改过的内容"


def test_render_offsets_are_self_consistent(service):
    """original_text 与 original_char_start/end 必须来自同一次渲染，否则区间指针会漂移。"""
    repo = Repository(service._components("work").db, service.segmenter)
    ids = repo.upsert_messages(dialogue_messages())

    text, offsets = repo.render_range(ids[0], ids[3])
    assert len(offsets) == 4
    for _msg_id, start, end in offsets:
        line = text[start:end]
        assert line.startswith(("我:", "助手:")), line

    # 整体区间应覆盖所有行
    span_start, span_end = repo.message_span(ids[0], ids[3])
    assert span_start == offsets[0][1]
    assert span_end == offsets[-1][2]
    assert text[span_start:span_end] == text

    # 与独立渲染函数一致
    rows = repo.get_message_range(ids[0], ids[3])
    assert render_messages_with_offsets(rows)[0] == text


def test_content_hash_normalizes_whitespace_and_case(service):
    assert content_hash("Hello  World") == content_hash("  hello world  ")
    assert content_hash("a") != content_hash("b")


def test_dedupe_finds_existing_chunk(service):
    repo = Repository(service._components("work").db, service.segmenter)
    draft = ChunkDraft(summary_text="同一条记忆内容")
    repo.insert_chunk(draft)
    found = repo.find_alive_by_hash(content_hash("同一条记忆内容"))
    assert found is not None
    # 软删除后不再算重复
    repo.soft_delete(found["chunk_uid"])
    assert repo.find_alive_by_hash(content_hash("同一条记忆内容")) is None


def test_soft_delete_and_restore(service):
    repo = Repository(service._components("work").db, service.segmenter)
    result = repo.insert_chunk(ChunkDraft(summary_text="待删除的记忆"))
    uid = result["uid"]

    assert repo.count_chunks(alive_only=True) == 1
    assert repo.soft_delete(uid) is True
    assert repo.count_chunks(alive_only=True) == 0
    assert repo.get_chunk(uid) is None, "默认查询应看不到已删除切片"
    assert repo.get_chunk(uid, include_deleted=True) is not None

    assert repo.restore(uid) is True
    assert repo.count_chunks(alive_only=True) == 1

    # 重复删除/恢复应返回 False 而不是抛错
    repo.soft_delete(uid)
    assert repo.soft_delete(uid) is False
    repo.restore(uid)
    assert repo.restore(uid) is False


def test_superseded_chunk_leaves_default_recall(service):
    """被取代的切片退出**召回**，但仍**保留可查**——这是"数据可调试"的体现，
    所以 get_chunk 仍能取到它，只有检索和计数把它排除在外。"""
    repo = Repository(service._components("work").db, service.segmenter)
    old = repo.insert_chunk(ChunkDraft(summary_text="旧版本记忆"))
    new = repo.insert_chunk(ChunkDraft(summary_text="新版本记忆"), supersede_uid=old["uid"])

    assert repo.count_chunks(alive_only=True) == 1
    assert repo.count_chunks(alive_only=False) == 2
    assert repo.get_chunk(old["uid"])["superseded_by"] == new["uid"]

    alive_ids = repo.alive_ids()
    assert repo.get_chunk(new["uid"])["id"] in alive_ids
    assert repo.get_chunk(old["uid"])["id"] not in alive_ids


def test_update_chunk_rebuilds_search_text(service):
    """search_text 是分词结果，所以按词项断言。"""
    repo = Repository(service._components("work").db, service.segmenter)
    result = repo.insert_chunk(ChunkDraft(summary_text="原始内容", title="原标题"))
    repo.update_chunk(result["uid"], summary_text="换成了完全不同的内容", title="新标题")

    row = repo.get_chunk(result["uid"])
    tokens = set(row["search_text"].split())
    assert "完全" in tokens and "不同" in tokens, row["search_text"]
    assert "新" in tokens or "新标题" in tokens
    assert "原始" not in tokens
    assert row["content_hash"] == content_hash("换成了完全不同的内容")


def test_update_chunk_rejects_unknown_fields(service):
    repo = Repository(service._components("work").db, service.segmenter)
    result = repo.insert_chunk(ChunkDraft(summary_text="内容"))
    with pytest.raises(ValueError):
        repo.update_chunk(result["uid"], embedding="nope")


def test_summary_truncated_flag_recorded(service):
    repo = Repository(service._components("work").db, service.segmenter)
    long_text = "很长的摘要内容" * 100
    result = repo.insert_chunk(ChunkDraft(summary_text=long_text), summary_soft_limit=50)
    row = repo.get_chunk(result["uid"])
    assert row["summary_truncated"] == 1
    assert service._components("work").repo.stats()["summary_truncated"] == 1


def test_embedding_mismatch_is_blocked_not_silently_ignored(service):
    """跨模型比较向量距离会返回看似有分数、实则全是噪声的结果，必须直接报错。"""
    db = service._components("work").db
    db.assert_compatible("BAAI/bge-m3", 1024)
    with pytest.raises(EmbeddingMismatchError) as excinfo:
        db.assert_compatible("other/model", 1024)
    assert "不匹配" in str(excinfo.value)

    with pytest.raises(EmbeddingMismatchError):
        db.assert_compatible("BAAI/bge-m3", 768)


def test_chunk_uid_is_stable_across_title_rename(service):
    """命名权：改 title 不改 uid。否则用户一改名，模型手上的 uid 就失效了。"""
    repo = Repository(service._components("work").db, service.segmenter)
    result = repo.insert_chunk(ChunkDraft(summary_text="内容", title="原名字"))
    uid = result["uid"]
    repo.update_chunk(uid, title="用户改的名字")
    assert repo.get_chunk(uid)["title"] == "用户改的名字"
    assert repo.get_chunk(uid)["chunk_uid"] == uid


def test_registry_rename_keeps_file_and_identity(service):
    registry: DatabaseRegistry = service.registry
    entry = registry.get("work")
    old_path = entry.file_path
    old_uuid = entry.db_uuid

    renamed = registry.rename("work", "工作")
    assert renamed.display_name == "工作"
    assert renamed.file_path == old_path, "不改名文件时路径应保持不变"
    assert renamed.db_uuid == old_uuid, "身份必须稳定"

    # 文件与身份脱钩：即使文件被改名，身份仍在文件内部
    assert registry.get("工作").db_uuid == old_uuid


def test_registry_refuses_duplicate_names(service):
    with pytest.raises(ValueError):
        service.registry.create("work", service.settings)


def test_slugify_keeps_cjk():
    assert slugify("工作") == "工作"
    assert slugify("my db") == "my_db"
    assert "/" not in slugify("a/b")
    assert slugify("   ") == "db"


def test_discover_adopts_copied_database_file(service):
    """把一个库文件拷进数据目录就应该能用——这是「一个文件 = 一份记忆」的自然延伸。

    必须走 snapshot_database（先 checkpoint 再拷）：WAL 模式下直接拷 .db
    会漏掉还在 -wal 里的写入，文件就不是自包含的了。
    """
    snapshot = service.settings.data_dir / "copied.db"
    service.snapshot_database("work", snapshot)
    assert snapshot.exists()

    adopted = service.registry.discover()
    assert "copied" in adopted
    assert service.registry.get("copied").db_uuid == service.registry.get("work").db_uuid


def test_legacy_relative_file_path_is_resolved_against_data_dir(service):
    """历史遗留的相对路径登记必须能打开。

    它当年是按**建库时的 cwd** 存下来的（形如 `data\\work.db`，相对项目根而非
    数据目录），而 MCP 子进程与 hook 的 cwd 由宿主决定——照 cwd 解析会指向一个
    不存在的位置，库直接打不开，且 hook 的失败是静默的（退出码恒为 0）。
    """
    import os

    entry = service.registry.get("work")
    real = service.registry.resolve_path(entry)
    entry.file_path = "data" + os.sep + "work.db"

    resolved = service.registry.resolve_path(entry)
    assert resolved == real
    db = service.registry.open_one("work", service.settings)
    try:
        assert db.count_chunks() >= 0  # 能真的打开，而不是抛 unable to open database file
    finally:
        db.close()


def test_discover_does_not_adopt_the_same_file_twice(service):
    """相对路径登记不能让 discover 把同一个文件认成新库。

    这正是重复条目（`work-2`）的来源：`known_files` 用 cwd 解析相对路径，
    换一个 cwd 跑就认不出同一个文件，于是原地不动的库被"再收养"一次——
    同一份记忆出现两个名字，规模、模型状态都要看两遍。
    """
    import os

    service.registry.get("work").file_path = "data" + os.sep + "work.db"

    adopted = service.registry.discover()

    assert adopted == []
    assert {e.display_name for e in service.registry.list()} == {"work"}


def test_discover_repoints_a_dangling_entry_instead_of_duplicating(service):
    """旧登记指向的文件不在了，但数据目录里还有同一份（db_uuid 相同）——应改指向。

    另起一个 `work-2` 会让一份记忆有两个身份，规模与模型状态都要看两遍。
    """
    good = service.registry.resolve_path(service.registry.get("work"))
    service.registry.get("work").file_path = str(
        service.settings.data_dir / "已经搬走了.db"
    )

    adopted = service.registry.discover()

    assert adopted == ["work"]
    assert service.registry.resolve_path(service.registry.get("work")) == good
    assert {e.display_name for e in service.registry.list()} == {"work"}


def test_checkpoint_makes_file_self_contained(service):
    """关闭库之后主文件应包含全部数据，单独拷走它不丢东西。"""
    import shutil
    import sqlite3

    service.store("检查点测试记忆内容", db="work")
    components = service._components("work")
    components.db.checkpoint()

    lone_copy = service.settings.data_dir / "lone.db"
    shutil.copy2(service.registry.get("work").file_path, lone_copy)
    conn = sqlite3.connect(lone_copy)
    count = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    conn.close()
    assert count == 1, "checkpoint 后主文件应自包含"
