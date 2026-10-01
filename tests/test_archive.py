"""导出/导入往返测试。

往返的核心承诺：还原出的库不是"长得像"的库，而是**同一份记忆**——
uid、消息 id、游标、概览状态、软删除与版本链全部保真，检索行为一致。

这里最该守住的不变式：

1. **消息 id 逐字保留**。`chunks.msg_id_start/end` 与游标指向它，重新编号
   会让全部区间指针失效（quote 模式读的是切片自带的 original_text，表面上看不出
   问题，window 模式才会炸——这类"表面正常"正是往返测试要堵的）。
2. **会话概览（L1）与 pending_changes 保真**。L1 是花模型调用生成的；
   pending 记录的是"概览落后多少"，还原时被清零的话，freshness 策略会
   误以为概览是最新的。
3. **检索一致性**。同样的查询在源库与还原库里给出同样的第一名。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import SESSION, WINDOW, dialogue_messages

from duramem.models import Message
from duramem.store.database import EmbeddingMismatchError

# 往返比对时不比内部自增 id：消息 id 是硬约束必须一致，切片/概览/运行记录
# 的 id 是新库重新分配的，本来就允许不同。
_CHUNK_FIELDS = (
    "chunk_uid",
    "title",
    "title_suggested",
    "summary_text",
    "original_text",
    "keywords",
    "source_db",
    "source_window",
    "source_session",
    "msg_id_start",
    "msg_id_end",
    "original_char_start",
    "original_char_end",
    "hit_count",
    "weight",
    "tags",
    "created_at",
    "updated_at",
    "deleted_at",
    "superseded_by",
    "summary_tokens",
    "summary_truncated",
)


@pytest.fixture
def rich(seeded):
    """在 seeded 之上把库做出"有状态"的样子：概览、软删、手动切片、游标。

    往返测试要还原的是状态而不只是行数，所以源库得先有状态可还原。
    """
    components = seeded._components("work")
    outcome = components.session_summarizer.summarize(WINDOW, SESSION)
    assert outcome.ok, outcome.warnings

    chunks = components.repo.list_chunks(limit=100)
    assert chunks, "seeded 库里应有切片"
    # 软删一条：deleted_at 要跟着归档走，还原后它仍然退出召回但可恢复
    seeded.forget(chunks[0]["chunk_uid"], db="work")

    manual = seeded.store(
        "手动记录：部署脚本在 scripts/deploy.bat，回滚用 rollback.bat",
        original_text="部署脚本在 scripts/deploy.bat，回滚用 rollback.bat",
        db="work",
        title="部署脚本",
        tags=["manual", "ops"],
    )
    assert manual["ok"] and not manual.get("duplicate")
    seeded.update_chunk(manual["uid"], db="work", weight=2.5, tags=["manual", "ops", "pinned"])
    return seeded


def test_roundtrip_restores_everything(rich, tmp_path):
    svc = rich
    payload = svc.export_database(db="work", with_messages=True)
    archive = tmp_path / "work-export.json"
    archive.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    assert payload["format_version"] == 2
    assert payload["cursors"], "总结过就有游标"
    assert payload["layers"], "生成过概览就有会话层"

    result = svc.import_archive(archive, db="restored")
    assert result["ok"], result
    assert result["warnings"] == []
    assert result["chunks"] == len(payload["chunks"])
    assert result["messages"] == len(payload["messages"])
    assert result["layers"] == len(payload["layers"])

    src = svc._components("work")
    dst = svc._components("restored")

    # 消息：逐字一致，id 不变（区间指针的根基）
    assert src.repo.all_messages() == dst.repo.all_messages()

    # 切片：身份与全部业务字段保真（含软删除、版本链、命中数、权重、标签）
    a = {
        c["chunk_uid"]: c
        for c in src.repo.list_chunks(include_deleted=True, include_superseded=True, limit=10_000)
    }
    b = {
        c["chunk_uid"]: c
        for c in dst.repo.list_chunks(include_deleted=True, include_superseded=True, limit=10_000)
    }
    assert set(a) == set(b)
    for uid, row in a.items():
        for field in _CHUNK_FIELDS:
            assert b[uid][field] == row[field], f"{uid}.{field}: {row[field]!r} != {b[uid][field]!r}"

    # 游标：原样（含指向的消息 id 与更新时间）
    assert src.repo.cursors() == dst.repo.cursors()

    # 会话层：L1/L0 正文、pending、覆盖度保真；派生字段按当前口径重算
    for layer in payload["layers"]:
        restored = dst.session_layers.get(layer["window_id"], layer["session_id"])
        assert restored is not None
        assert restored["overview_text"] == layer["overview_text"]
        assert restored["abstract_text"] == layer["abstract_text"]
        assert restored["pending_changes"] == layer["pending_changes"]
        assert restored["coverage_total"] == layer["coverage_total"]
        assert restored["overview_digest"] is not None  # 按当前实现重算

    # 库的出生时间跟着记忆走，不是导入那一刻
    assert dst.db.meta["created_at"] == src.db.meta["created_at"]
    # 副本要有新身份，否则两个文件共一个 uuid，discover 会认错
    assert dst.db.meta["db_uuid"] != src.db.meta["db_uuid"]

    # 检索一致性：同一条查询在两个库命中同一第一名（uid 寻址跨库有效）
    query = "ERR_CONN_REFUSED_0x7f 怎么解决"
    hits_src = svc.search(query, db="work").hits
    hits_dst = svc.search(query, db="restored").hits
    assert hits_src and hits_dst
    assert hits_src[0].uid == hits_dst[0].uid

    # 原文回查一致性：同一 uid 在两个库读出同样的原文
    alive = [c for c in b.values() if c["deleted_at"] is None and c["original_text"]]
    target = next(c for c in alive if c["msg_id_start"] is not None)
    quote_src = svc.read_original(target["chunk_uid"], db="work")
    quote_dst = svc.read_original(target["chunk_uid"], db="restored")
    assert quote_src.ok and quote_dst.ok
    assert quote_dst.text == quote_src.text
    window_dst = svc.read_original(target["chunk_uid"], mode="window", window=1, db="restored")
    window_src = svc.read_original(target["chunk_uid"], mode="window", window=1, db="work")
    assert window_dst.ok and window_dst.text == window_src.text

    # 会话概览读取：还原库里 dm_read_session 照常工作
    read = svc.read_session(SESSION, detail="overview", db="restored")
    assert read.ok, read.warnings


def test_roundtrip_without_messages_warns(rich, tmp_path):
    """v1 归档形态：没带 messages。导入必须成功，但要把代价说清楚。"""
    svc = rich
    payload = svc.export_database(db="work", with_messages=False)
    archive = tmp_path / "work-export.json"
    archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = svc.import_archive(archive, db="restored")
    assert result["ok"]
    assert any("原始消息" in w for w in result["warnings"])


def test_dry_run_writes_nothing(rich, tmp_path):
    svc = rich
    payload = svc.export_database(db="work", with_messages=True)
    archive = tmp_path / "work-export.json"
    archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = svc.import_archive(archive, db="restored", dry_run=True)
    assert result["ok"] and result["dry_run"]
    assert result["chunks"] == len(payload["chunks"])

    assert not svc.registry.exists("restored")
    assert not list(svc.settings.data_dir.glob("restored*.db"))


def test_refuses_existing_db_name(rich, tmp_path):
    svc = rich
    payload = svc.export_database(db="work")
    archive = tmp_path / "work-export.json"
    archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(Exception, match="库已存在"):
        svc.import_archive(archive, db="work")
    with pytest.raises(Exception, match="库已存在"):
        # 归档里的 db 名就是 work，同样不许覆盖
        svc.import_archive(archive)


def test_refuses_garbage_and_missing_files(tmp_path, settings):
    from duramem.service import DuramemError, Service

    svc = Service(settings)
    try:
        with pytest.raises(DuramemError, match="归档文件不存在"):
            svc.import_archive(tmp_path / "nope.json")

        garbage = tmp_path / "garbage.json"
        garbage.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
        with pytest.raises(DuramemError, match="不是 Duramem 导出归档"):
            svc.import_archive(garbage)

        newer = tmp_path / "newer.json"
        newer.write_text(
            json.dumps({"format": "duramem-export", "format_version": 99}), encoding="utf-8"
        )
        with pytest.raises(DuramemError, match="版本更新"):
            svc.import_archive(newer)
    finally:
        svc.close()


def test_no_reindex_leaves_loud_not_silent_state(rich, tmp_path):
    """跳过向量重建时，库必须处于"大声拒绝"而不是"静默只有词法路"的状态。"""
    svc = rich
    payload = svc.export_database(db="work", with_messages=True)
    archive = tmp_path / "work-export.json"
    archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = svc.import_archive(archive, db="restored", reindex=False)
    assert result["ok"]
    assert any("rebuild-vectors" in w for w in result["warnings"])

    # 直接开库看元数据，不经 _components——那条路会先做一致性校验，
    # 拿到的是"期望中的拒绝"，看不到元数据本身
    from duramem.store.database import Database

    entry = svc.registry.get("restored")
    database = Database(Path(entry.file_path), wal=svc.settings.sqlite_wal)
    try:
        meta = database.meta
    finally:
        database.close()
    assert meta.get("vectors_stale") == "1"
    assert meta.get("vector_source") == "archive-import"

    with pytest.raises(EmbeddingMismatchError):
        svc.search("anything", db="restored")

    # 补建之后一切正常——错误信息里承诺的补救路径必须真的通
    svc.rebuild_vectors("restored")
    hits = svc.search("ERR_CONN_REFUSED_0x7f", db="restored").hits
    assert hits


def test_empty_library_roundtrip(service, tmp_path):
    """空库也能导出导入——备份/迁移场景里"什么都还没记"是合法状态。"""
    svc = service
    payload = svc.export_database(db="work", with_messages=True)
    archive = tmp_path / "empty-export.json"
    archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = svc.import_archive(archive, db="restored")
    assert result["ok"], result
    assert result["chunks"] == 0
    assert svc._components("restored").repo.count_chunks() == 0


def test_multi_session_roundtrip(service, tmp_path):
    """两个会话各自总结、各自有游标与概览——还原后互不串。

    第二个会话必须是**不同内容**：完全相同的对话会被 L0 去重判为重复、
    一个切片都不落库（去重按设计工作，测试不能反着依赖它）。
    """
    svc = service
    svc.ingest(dialogue_messages(), db="work", auto_summarize=False)
    other = [
        ("user", "前端列表分页怎么做"),
        ("assistant", "用 TanStack Table 自带分页，设置 pageSize 就行"),
        ("user", "服务端分页呢"),
        ("assistant", "传 page 和 limit 参数，接口返回 total，前端手动拼 pageCount"),
    ]
    svc.ingest(
        [Message("win-2", "chat-B", i, role, content) for i, (role, content) in enumerate(other)],
        db="work",
        auto_summarize=False,
    )
    for window, session in ((WINDOW, SESSION), ("win-2", "chat-B")):
        outcome = svc.summarize(window, session, db="work")
        assert outcome.ok, outcome.warnings
        assert outcome.added > 0, f"{session} 应产出切片"
        layer = svc._components("work").session_summarizer.summarize(window, session)
        assert layer.ok, layer.warnings

    payload = svc.export_database(db="work", with_messages=True)
    archive = tmp_path / "multi-export.json"
    archive.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = svc.import_archive(archive, db="restored")
    assert result["ok"] and result["warnings"] == []

    src = svc._components("work")
    dst = svc._components("restored")
    assert src.repo.cursors() == dst.repo.cursors()
    assert len(dst.session_layers.export_rows()) == len(src.session_layers.export_rows()) == 2
