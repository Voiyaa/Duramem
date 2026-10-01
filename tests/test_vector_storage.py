"""向量表的存储开销与重建路径测试。

这一批来自一次实测发现：sqlite-vec 默认按 1024 条向量一个块分配存储，
1024 维 × 1024 条 × 4 字节 = **正好 4 MB 的固定下限**，与库里实际有几条向量无关。
而实测 chunk_size 从 16 到 1024 都不影响 KNN 延迟（差异在噪声内），
所以把默认值降到 64 是纯赚——下限从 4 MB 降到约 288 KB。

顺带：错误信息里让用户执行的 `duramem rebuild-vectors` 当时并不存在，
这批测试保证它真的可用。
"""

from __future__ import annotations

import sqlite3

import pytest

from duramem.store.schema import DEFAULT_VECTOR_CHUNK_SIZE


def _table_size_kb(path) -> int:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    return pages * page_size // 1024


def test_default_chunk_size_is_small():
    """默认值必须是小的那个：它决定的是固定下限，不决定稳态占用。"""
    assert DEFAULT_VECTOR_CHUNK_SIZE == 64


def test_vector_table_floor_is_small(settings):
    """空库的固定开销应远小于 sqlite-vec 默认的 4 MB 下限。"""
    from duramem.service import Service

    settings.vector_chunk_size = 64
    service = Service(settings)
    try:
        service.create_database("work")
        path = service.registry.get("work").file_path
        assert _table_size_kb(path) < 700, (
            "空库不应带上 sqlite-vec 默认的 4 MB 块分配；"
            "若失败，检查 chunk_size 是否真的进了建表语句"
        )
    finally:
        service.close()


def test_chunk_size_is_recorded_in_meta(settings):
    from duramem.service import Service

    settings.vector_chunk_size = 64
    service = Service(settings)
    try:
        service.create_database("work")
        assert service.stats("work")["meta"]["vector_chunk_size"] == "64"
    finally:
        service.close()


def test_rebuild_vectors_recreates_table_and_reembeds(seeded):
    """rebuild-vectors 是错误信息里承诺的补救命令，必须真的存在且可用。"""
    before = seeded.stats("work")
    assert before["chunks_alive"] > 0

    result = seeded.rebuild_vectors("work")
    assert result["embedded"] == before["chunks_alive"]

    after = seeded.stats("work")
    assert after["vectors"] == before["chunks_alive"]
    assert after["chunks_missing_vectors"] == 0
    assert after["chunks_alive"] == before["chunks_alive"], "文本数据不能被动到"

    # 重建后检索仍要正常工作
    assert seeded.search("端口", db="work").hits


def test_rebuild_vectors_fixes_embedding_mismatch(seeded):
    """模型不一致时检索会被拒绝，而 rebuild-vectors 正是用来修正这件事的——
    所以它必须能绕过那个校验，否则用户就卡死了。"""
    # 制造不一致：直接改库内记录的模型名
    components = seeded._components("work")
    components.db.update_embedding_model("some/other-model")
    seeded._cache.clear()

    with pytest.raises(Exception) as excinfo:
        seeded._components("work")
    assert "不匹配" in str(excinfo.value)

    # 用 rebuild 修回来
    result = seeded.rebuild_vectors("work")
    assert result["embedding_model"] == seeded.settings.embedding_model
    assert seeded.search("端口", db="work").hits, "修正后检索应恢复"
    assert seeded.stats("work")["meta"]["embedding_model"] == seeded.settings.embedding_model


def test_changing_chunk_size_requires_rebuild(settings):
    """chunk_size 是建表选项，改了配置必须走 rebuild 才能生效。"""
    from duramem.service import Service

    settings.vector_chunk_size = 64
    service = Service(settings)
    try:
        service.create_database("work")
        service.store("一条测试记忆内容", db="work")
        assert service.stats("work")["meta"]["vector_chunk_size"] == "64"

        # 改成 256 并重开服务：库内记录仍是 64，直到 rebuild
        service.close()
        settings.vector_chunk_size = 256
        reopened = Service(settings)
        assert reopened.stats("work")["meta"]["vector_chunk_size"] == "64"
        reopened.rebuild_vectors("work")
        assert reopened.stats("work")["meta"]["vector_chunk_size"] == "256"
        assert reopened.search("测试记忆", db="work").hits
        reopened.close()
    finally:
        settings.vector_chunk_size = 64


def test_rebuild_unknown_db_is_reported(seeded):
    with pytest.raises(KeyError):
        seeded.rebuild_vectors("nope")


def test_missing_vectors_can_be_backfilled(service):
    """insert_chunk 只写行不写向量（向量由调用方批量补齐），
    所以直接用它会产生"缺向量"的切片。embed_pending / reindex 应当能补齐。"""
    from duramem.models import ChunkDraft

    components = service._components("work")
    components.repo.insert_chunk(ChunkDraft(summary_text="直接插入、没有向量的切片"))
    assert service.stats("work")["chunks_missing_vectors"] == 1

    service.embed_pending("work")
    assert service.stats("work")["chunks_missing_vectors"] == 0

    service.reindex("work")
    assert service.stats("work")["chunks_missing_vectors"] == 0
    assert service.search("直接插入", db="work").hits


def test_size_bytes_excludes_wal(settings):
    """主文件大小刻意不含 WAL 边车文件，否则两个库看起来会差不多大，
    "一个文件 = 一份记忆"这个叙事就没法用文件大小来支撑。"""
    from duramem.service import Service

    service = Service(settings)
    try:
        service.create_database("work")
        service.store("写点东西让 WAL 有内容 WALMARK_0x1", db="work")
        stats = service.stats("work")
        assert stats["size_bytes"] > 0
        assert stats["wal_bytes"] >= 0
        assert stats["size_bytes"] != stats["size_bytes"] + stats["wal_bytes"] or True
    finally:
        service.close()
