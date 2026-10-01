"""schema 迁移（v1 → v2）。

这是本项目第一个迁移，也是最容易静默弄坏数据的一步：列改名走的是
`ALTER TABLE ... RENAME COLUMN`，而 chunks 表挂着一张 external-content 的
FTS5 表和三支触发器。改名前后 FTS 是否仍与内容表一致，必须实测而不是推理。

测试全部离线：手写 v1 的 DDL 造一个"存量库"，再走正常的打开路径（
`Database.initialize` → `init_database` → `migrate`）。
"""

from __future__ import annotations

import sqlite3

from duramem.store.database import Database
from duramem.store.schema import SCHEMA_VERSION, read_meta

L2_TEXT = "user: 原始消息 ERR_0x7f"
L1_SEARCH = "v1 摘要 ERR_0x7f v1 标题"

# v1 时代的形态：原文列叫 l1_text，字符偏移叫 char_start/char_end，没有
# session_layers。**刻意照抄 v1 的 DDL 而不是引用当前 schema**——测试要验证的
# 正是"老库能不能升上来"，引用当前定义会让它跟着实现一起变，从此失去意义。
_V1_DDL = """
CREATE TABLE db_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE messages (
    id            INTEGER PRIMARY KEY,
    window_id     TEXT    NOT NULL,
    session_id    TEXT    NOT NULL,
    seq           INTEGER NOT NULL,
    role          TEXT    NOT NULL,
    speaker       TEXT,
    content       TEXT    NOT NULL,
    ts            TEXT,
    source_msg_id TEXT,
    content_hash  TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    UNIQUE (window_id, session_id, seq)
);

CREATE TABLE chunks (
    id              INTEGER PRIMARY KEY,
    chunk_uid       TEXT    NOT NULL UNIQUE,
    title           TEXT    NOT NULL DEFAULT '',
    title_suggested TEXT,
    l0_text         TEXT    NOT NULL,
    search_text     TEXT    NOT NULL DEFAULT '',
    l1_text         TEXT    NOT NULL DEFAULT '',
    keywords        TEXT    NOT NULL DEFAULT '[]',
    source_db       TEXT,
    source_window   TEXT,
    source_session  TEXT,
    msg_id_start    INTEGER,
    msg_id_end      INTEGER,
    char_start      INTEGER,
    char_end        INTEGER,
    content_hash    TEXT    NOT NULL,
    hit_count       INTEGER NOT NULL DEFAULT 0,
    weight          REAL    NOT NULL DEFAULT 1.0,
    tags            TEXT    NOT NULL DEFAULT '[]',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    deleted_at      TEXT,
    superseded_by   TEXT,
    l0_tokens       INTEGER NOT NULL DEFAULT 0,
    l0_truncated    INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    search_text,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;
CREATE TRIGGER chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, search_text)
        VALUES ('delete', old.id, old.search_text);
END;
CREATE TRIGGER chunks_fts_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, search_text)
        VALUES ('delete', old.id, old.search_text);
    INSERT INTO chunks_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;
"""


def _make_v1_db(path, *, with_version: bool = True) -> None:
    """造一个 v1 形态的存量库，含一条消息与一条切片（带区间指针）。"""
    conn = sqlite3.connect(path)
    conn.executescript(_V1_DDL)

    meta = {
        "db_uuid": "v1uuid",
        "created_at": "2026-01-01T00:00:00+00:00",
        "embedding_model": "BAAI/bge-m3",
        "embedding_dim": "1024",
        "vector_chunk_size": "64",
    }
    # 绝大多数 v1 库有这个键；但更老的库可能没有——`init_database` 只在
    # db_meta 为**空**时写初始键，所以后加的键不会补进老库。两种都要能升。
    if with_version:
        meta["schema_version"] = "1"
    conn.executemany("INSERT INTO db_meta(key, value) VALUES (?, ?)", list(meta.items()))

    conn.execute(
        "INSERT INTO messages(id, window_id, session_id, seq, role, content,"
        " content_hash, created_at) VALUES (1,'win','sess',0,'user',?,'h1',"
        "'2026-01-01T00:00:00+00:00')",
        (L2_TEXT.replace("user: ", ""),),
    )
    conn.execute(
        "INSERT INTO chunks(id, chunk_uid, title, l0_text, search_text, l1_text,"
        " source_window, source_session,"
        " msg_id_start, msg_id_end, char_start, char_end, content_hash,"
        " created_at, updated_at)"
        " VALUES (1,'uidV1','v1 标题','v1 摘要 ERR_0x7f',?,?,"
        " 'win','sess',1,1,0,?,'h2',"
        "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
        (L1_SEARCH, L2_TEXT, len(L2_TEXT)),
    )
    conn.commit()
    conn.close()


def _open(path) -> Database:
    """走正常打开路径，让 init_database → migrate 跑起来。"""
    db = Database(path, wal=False)
    db.initialize(
        db_uuid="whatever",
        created_at="2026-09-24T00:00:00+00:00",
        embedding_model="BAAI/bge-m3",
        embedding_dim=1024,
        summary_soft_limit_tokens=256,
    )
    return db


def _columns(db: Database, table: str) -> set[str]:
    return {str(r[1]) for r in db.read_conn.execute(f"PRAGMA table_info({table})")}


def _fts_ok(db: Database, table: str) -> None:
    """FTS5 自带的 external-content 一致性检查，不一致会抛异常。"""
    db.read_conn.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")


# ============================================================== 列改名与数据


def test_v1_columns_are_renamed(tmp_path):
    path = tmp_path / "v1.db"
    _make_v1_db(path)
    db = _open(path)
    cols = _columns(db, "chunks")
    assert "original_text" in cols and "l1_text" not in cols and "l2_text" not in cols
    assert "original_char_start" in cols and "char_start" not in cols
    assert "original_char_end" in cols and "char_end" not in cols
    db.close()


def test_v1_data_survives_migration(tmp_path):
    """改名不是重建：内容与区间指针必须原样保留。"""
    path = tmp_path / "v1.db"
    _make_v1_db(path)
    db = _open(path)
    row = db.read_conn.execute(
        "SELECT original_text, original_char_start, original_char_end, summary_text, chunk_uid FROM chunks"
    ).fetchone()
    assert row["original_text"] == L2_TEXT
    assert (row["original_char_start"], row["original_char_end"]) == (0, len(L2_TEXT))
    assert row["summary_text"] == "v1 摘要 ERR_0x7f"
    assert row["chunk_uid"] == "uidV1"
    # 区间指针自洽性不变（这个不变式是 tests/test_summarizer.py 全量校验的那条）
    assert row["original_text"][row["original_char_start"] : row["original_char_end"]] == row["original_text"]
    db.close()


def test_v1_fts_index_still_matches_after_rename(tmp_path):
    """改名后 FTS 仍与内容表一致——这是最容易静默失败的一处。"""
    path = tmp_path / "v1.db"
    _make_v1_db(path)
    db = _open(path)
    _fts_ok(db, "chunks_fts")
    hit = db.read_conn.execute(
        "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'ERR_0x7f'"
    ).fetchone()[0]
    assert hit == 1
    db.close()


def test_triggers_still_sync_after_rename(tmp_path):
    """改名后 INSERT / UPDATE 触发器必须照常维护 FTS。"""
    path = tmp_path / "v1.db"
    _make_v1_db(path)
    db = _open(path)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO chunks(chunk_uid, summary_text, search_text, original_text,"
            " content_hash, created_at, updated_at)"
            " VALUES ('uidNew','新摘要 ZZZ_NEW','新摘要 ZZZ_NEW','新原文',"
            "'h3','2026-09-24T00:00:00+00:00','2026-09-24T00:00:00+00:00')"
        )
    assert (
        db.read_conn.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'ZZZ_NEW'"
        ).fetchone()[0]
        == 1
    )

    with db.write() as conn:
        conn.execute("UPDATE chunks SET search_text='改过的 RRR_OLD' WHERE chunk_uid='uidV1'")
    # 旧词随 UPDATE 触发器从索引里撤掉，新词进来
    assert (
        db.read_conn.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'ERR_0x7f'"
        ).fetchone()[0]
        == 0
    )
    assert (
        db.read_conn.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'RRR_OLD'"
        ).fetchone()[0]
        == 1
    )
    _fts_ok(db, "chunks_fts")
    db.close()


# ============================================================== 幂等与表


def test_migration_is_idempotent(tmp_path):
    """三个进程会各自打开同一个库，重复迁移必须无副作用。"""
    path = tmp_path / "v1.db"
    _make_v1_db(path)
    for _ in range(3):
        db = _open(path)
        row = db.read_conn.execute("SELECT original_text FROM chunks").fetchone()
        assert row["original_text"] == L2_TEXT
        _fts_ok(db, "chunks_fts")
        db.close()


def test_session_tables_are_created_on_v1_upgrade(tmp_path):
    """会话层两表（内容 + 向量）要在存量库上一并建起来。"""
    path = tmp_path / "v1.db"
    _make_v1_db(path)
    db = _open(path)
    names = {
        str(r[0])
        for r in db.read_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "session_layers" in names
    assert "session_layers_fts" in names
    assert "session_layers_vec" in names
    db.close()


def test_session_fts_triggers_work_on_v1_upgrade(tmp_path):
    """会话层的 FTS 触发器也要能正常同步。"""
    path = tmp_path / "v1.db"
    _make_v1_db(path)
    db = _open(path)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO session_layers(window_id, session_id, abstract_text, overview_text,"
            " search_text, created_at, updated_at)"
            " VALUES ('win','sess','会话摘要 KKK','# 会话概览','会话摘要 KKK 会话概览',"
            "'2026-09-24T00:00:00+00:00','2026-09-24T00:00:00+00:00')"
        )
    assert (
        db.read_conn.execute(
            "SELECT count(*) FROM session_layers_fts WHERE session_layers_fts MATCH 'KKK'"
        ).fetchone()[0]
        == 1
    )
    _fts_ok(db, "session_layers_fts")
    db.close()


def test_schema_version_is_recorded(tmp_path):
    """迁移后库里记的版本要跟上，且缺键的老库也会被补上。"""
    for with_version in (True, False):
        path = tmp_path / f"v1-{with_version}.db"
        _make_v1_db(path, with_version=with_version)
        db = _open(path)
        assert read_meta(db.read_conn).get("schema_version") == SCHEMA_VERSION
        db.close()


def test_upgraded_db_keeps_identity_and_embedding_meta(tmp_path):
    """迁移不能覆盖库身份与嵌入元数据——那会让库看起来换了模型。"""
    path = tmp_path / "v1.db"
    _make_v1_db(path)
    db = _open(path)
    meta = read_meta(db.read_conn)
    assert meta["db_uuid"] == "v1uuid"  # 不是 initialize() 传进去的 "whatever"
    assert meta["embedding_model"] == "BAAI/bge-m3"
    assert meta["embedding_dim"] == "1024"
    db.close()


# ============================================================== 全新库


def test_fresh_db_is_born_at_v3(tmp_path):
    """新建库直接是 v3 形态，不需要迁移，且 db_meta 完整。"""
    path = tmp_path / "fresh.db"
    db = Database(path, wal=False)
    db.initialize(
        db_uuid="freshuuid",
        created_at="2026-09-24T00:00:00+00:00",
        embedding_model="BAAI/bge-m3",
        embedding_dim=1024,
        summary_soft_limit_tokens=256,
    )
    cols = _columns(db, "chunks")
    assert {"original_text", "original_char_start", "original_char_end"} <= cols
    assert "overview_text" not in cols
    meta = read_meta(db.read_conn)
    # migrate 不能抢先写 schema_version 而让初始化块被跳过（那会让这些键全缺）
    assert meta["db_uuid"] == "freshuuid"
    assert meta["embedding_model"] == "BAAI/bge-m3"
    assert meta["embedding_dim"] == "1024"
    assert meta["schema_version"] == SCHEMA_VERSION
    db.close()


def test_replace_vector_table_rebuilds_both_vector_tables(tmp_path):
    """重建向量时必须连会话向量表一起换，否则库里会混两种来源的向量。"""
    path = tmp_path / "fresh.db"
    db = Database(path, wal=False)
    db.initialize(
        db_uuid="freshuuid",
        created_at="2026-09-24T00:00:00+00:00",
        embedding_model="BAAI/bge-m3",
        embedding_dim=1024,
        summary_soft_limit_tokens=256,
    )

    def vec_sql(name: str) -> str:
        row = db.read_conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()
        return str(row["sql"]) if row else ""

    assert "FLOAT[1024]" in vec_sql("chunks_vec")
    assert "FLOAT[1024]" in vec_sql("session_layers_vec")

    db.replace_vector_table(8, 16)

    assert "FLOAT[8]" in vec_sql("chunks_vec")
    assert "FLOAT[8]" in vec_sql("session_layers_vec")
    assert read_meta(db.read_conn)["embedding_dim"] == "8"
    db.close()

# ============================================================== 真实打开路径


def _register_v1_db(data_dir, name: str = "old"):
    """在 data_dir 里登记一个 v1 存量库，返回它的路径。"""
    import json

    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / f"{name}.db"
    _make_v1_db(db_path)
    (data_dir / "registry.json").write_text(
        json.dumps(
            {
                "version": 1,
                "databases": {
                    name: {
                        "display_name": name,
                        "file_path": str(db_path),
                        "db_uuid": "v1uuid",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return db_path


def test_open_path_migrates_existing_db(tmp_path):
    """存量库走**打开**路径也必须升级——这才是生产里真正走的路径。

    这条是实测发现 bug 之后补的：迁移最初挂在 `init_database` 上，而
    `Database.initialize` 只在**建库**时被调用（`registry.create`）。正常打开
    走 `registry.open_one`，于是存量库打开后仍是旧列名，迁移形同虚设。
    直接调 `migrate()` 或 `initialize()` 的测试都发现不了这一点。
    """
    from duramem.config import Settings
    from duramem.service import Service

    data_dir = tmp_path / "data"
    _register_v1_db(data_dir)

    settings = Settings()
    settings.data_dir = data_dir
    settings.embedding_fake = True
    svc = Service(settings)
    try:
        comps = svc._components("old", verify=False)
        cols = {str(r[1]) for r in comps.db.read_conn.execute("PRAGMA table_info(chunks)")}
        assert "original_text" in cols, "打开即应完成列改名"
        assert "overview_text" not in cols
        assert "session_layers" in {
            str(r[0])
            for r in comps.db.read_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        svc.close()


def test_search_works_on_a_migrated_db(tmp_path):
    """升级后的存量库要能真的用起来：检索命中、原文可回查。"""
    from duramem.config import Settings
    from duramem.service import Service

    data_dir = tmp_path / "data"
    _register_v1_db(data_dir)

    settings = Settings()
    settings.data_dir = data_dir
    settings.embedding_fake = True
    svc = Service(settings)
    try:
        res = svc.search("ERR_0x7f", db="old")
        assert res.hits, "旧库里的切片在升级后应仍可被检索到"
        hit = res.hits[0]
        assert hit.uid == "uidV1"
        assert hit.session_id == "sess", "命中要带会话标识，模型据此去取会话概览"

        read = svc.read_original(hit.uid, db="old", mode="full", detail="full")
        assert read.ok
        assert read.text == L2_TEXT, "回查应走改名后的 original_text"
    finally:
        svc.close()


def test_repeated_opens_are_cheap_and_idempotent(tmp_path):
    """反复打开同一个库不该重复迁移、不该破坏数据。"""
    from duramem.config import Settings
    from duramem.store import registry as registry_module

    data_dir = tmp_path / "data"
    _register_v1_db(data_dir)
    settings = Settings()
    settings.data_dir = data_dir
    settings.embedding_fake = True

    reg = registry_module.get_registry(settings)
    for _ in range(3):
        db = reg.open_one("old", settings)
        try:
            row = db.read_conn.execute(
                "SELECT original_text, original_char_start, original_char_end FROM chunks"
            ).fetchone()
            assert row["original_text"] == L2_TEXT
            assert row["original_text"][row["original_char_start"] : row["original_char_end"]] == L2_TEXT
            _fts_ok(db, "chunks_fts")
        finally:
            db.close()


def test_cli_init_honours_data_dir(tmp_path, monkeypatch):
    """回归：`init --data-dir X` 必须建在 X，不能建在默认数据目录。

    这是实测踩出来的：cmd_init 当时调用 `_open_service()` 时没把 data_dir 传下去，
    于是 `--data-dir` 被静默忽略，默认目录里凭空多出一个空库并写进 registry。
    测试用的临时目录看起来"没生效"，实际是建错了地方。
    """
    import argparse

    from duramem.__main__ import cmd_init
    from duramem.config import reset_settings

    target = tmp_path / "explicit"
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "default"))
    reset_settings()
    try:
        code = cmd_init(
            argparse.Namespace(db="work", data_dir=str(target))
        )
        assert code == 0
        assert (target / "registry.json").exists(), "库该建在 --data-dir 指向的目录"
        assert (target / "work.db").exists()
        # 默认目录不该被牵扯进来
        assert not (tmp_path / "default" / "work.db").exists()
    finally:
        reset_settings()
