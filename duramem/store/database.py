"""单库连接管理：sqlite-vec 加载、WAL、写串行化、线程内读连接。"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Literal

import sqlite_vec

from duramem.store.schema import (
    DEFAULT_VECTOR_CHUNK_SIZE,
    META_VECTOR_SOURCE,
    META_VECTORS_STALE,
    EmbeddingMismatchError,
    assert_embedding_compatible,
    clear_vectors_stale,
    create_session_vector_table,
    create_vector_table,
    drop_session_vector_table,
    drop_vector_table,
    ensure_schema,
    init_database,
    mark_vectors_stale,
    read_meta,
    update_meta,
)


def _configure(conn: sqlite3.Connection, wal: bool) -> None:
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row


class Database:
    """一个 SQLite 记忆库。

    并发策略：读走线程内独立连接（WAL 下与写并发无阻），写走单一连接 + 锁串行化。
    这样既不会出现 `database is locked`，也不会让 KNN 之类的耗时读阻塞写入。
    """

    def __init__(self, path: Path, wal: bool = True) -> None:
        self.path = Path(path)
        self.wal = wal
        self._write_lock = threading.RLock()
        self._local = threading.local()
        self._write_conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ 连接

    @property
    def write_conn(self) -> sqlite3.Connection:
        if self._write_conn is None:
            with self._write_lock:
                if self._write_conn is None:
                    conn = sqlite3.connect(self.path, check_same_thread=False)
                    _configure(conn, self.wal)
                    self._write_conn = conn
        return self._write_conn

    @property
    def read_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path)
            _configure(conn, self.wal)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """关闭连接前先做 WAL checkpoint。

        这不是清理，而是产品语义：核心卖点是"一个文件 = 一份记忆"——
        用户会直接拷贝 .db 文件。WAL 模式下未 checkpoint 的数据在 -wal 里，
        只拷主文件会丢数据。关闭时收干净，文件才是自包含的。
        """
        with self._write_lock:
            if self._write_conn is not None:
                try:
                    if self.wal:
                        self._write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                self._write_conn.close()
                self._write_conn = None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def checkpoint(self) -> None:
        """把 WAL 内容并入主文件。用于安全的文件级拷贝/打包。"""
        if not self.wal:
            return
        with self._write_lock:
            try:
                self.write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

    def snapshot_to(self, target: Path) -> Path:
        """checkpoint 后把库文件安全复制到目标路径。

        比让用户手工拷 .db 更可靠：直接拷一个正在使用的 WAL 库会漏掉最近写入。
        """
        self.checkpoint()
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path, target)
        return target

    # ------------------------------------------------------------------ 初始化

    def initialize(
        self,
        db_uuid: str,
        created_at: str,
        embedding_model: str,
        embedding_dim: int,
        summary_soft_limit_tokens: int,
        reranker_model: str = "",
        vector_chunk_size: int = DEFAULT_VECTOR_CHUNK_SIZE,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            init_database(
                self.write_conn,
                db_uuid=db_uuid,
                created_at=created_at,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                summary_soft_limit_tokens=summary_soft_limit_tokens,
                reranker_model=reranker_model,
                vector_chunk_size=vector_chunk_size,
            )
            self.write_conn.commit()

    def ensure_schema(
        self,
        fallback_dim: int = 0,
        fallback_chunk_size: int = DEFAULT_VECTOR_CHUNK_SIZE,
    ) -> list[str]:
        """把库升到当前 schema，返回迁移动作（空列表 = 无需迁移）。

        **打开库时必须调用**，不能只在建库时调用 `initialize`——存量库的列改名
        与新增表只能靠这里发生。见 `schema.ensure_schema` 的说明。
        """
        with self._write_lock:
            actions = ensure_schema(self.write_conn, fallback_dim, fallback_chunk_size)
            self.write_conn.commit()
            return actions

    def replace_vector_table(self, dim: int, chunk_size: int) -> None:
        """删掉并重建向量表，同时更新库内记录的维度与块大小。

        用于换嵌入模型（维度可能变）、或改 `VECTOR_CHUNK_SIZE`
        （chunk_size 是建表选项，只能重建，不能原地改）。
        这是唯一会**绕过**模型一致性校验的路径——因为它的目的就是修正不一致。
        重建后向量是空的，需要紧接一次 reindex。

        会话层向量表必须一起换掉：两张表用同一个嵌入提供方，只换一张会让库里
        同时存在两种来源的向量——正是 `vector_source` 指纹要防的那种静默混用。
        """
        with self._write_lock:
            drop_vector_table(self.write_conn)
            create_vector_table(self.write_conn, dim, chunk_size)
            drop_session_vector_table(self.write_conn)
            create_session_vector_table(self.write_conn, dim, chunk_size)
            update_meta(self.write_conn, embedding_dim=str(dim), vector_chunk_size=str(chunk_size))
            self.write_conn.commit()

    def update_embedding_model(self, model: str, provider: str = "") -> None:
        """改了库内记录的嵌入模型与来源指纹。配合 replace_vector_table 使用。

        `provider` 是**实际生效**的提供方（如 `offline-hashing-1024`），
        与 `model`（配置里写的名字）可能不同。传了就一并记录并解除作废标记——
        重建完成后库里那批向量的来源就是它。
        """
        with self._write_lock:
            if provider:
                clear_vectors_stale(
                    self.write_conn, source=provider, model=model, dim=self.embedding_dim
                )
            else:
                update_meta(self.write_conn, embedding_model=model)
            self.write_conn.commit()

    def update_meta(self, **values: str) -> None:
        """写任意 db_meta 键值（如冷启动注入配置）。库级属性随文件走。"""
        with self.write() as conn:
            update_meta(conn, **values)

    def mark_vectors_stale(self, source: str) -> None:
        """见 `schema.mark_vectors_stale`：标记已有向量与当前配置不可比。"""
        with self._write_lock:
            mark_vectors_stale(self.write_conn, source)
            self.write_conn.commit()

    def count_chunks(self) -> int:
        """这里只用原始 SQL 数一下，避免为了一个计数去构造 Repository。"""
        try:
            row = self.read_conn.execute("SELECT count(*) FROM chunks").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------ 元数据

    @property
    def meta(self) -> dict[str, str]:
        return read_meta(self.read_conn)

    @property
    def db_uuid(self) -> str:
        return self.meta.get("db_uuid", "")

    @property
    def embedding_dim(self) -> int:
        return int(self.meta.get("embedding_dim") or 0)

    @property
    def vector_source(self) -> str:
        """库内向量实际由谁生成。空表示是无此字段的旧库。"""
        return self.meta.get(META_VECTOR_SOURCE) or ""

    @property
    def vectors_stale(self) -> bool:
        return (self.meta.get(META_VECTORS_STALE) or "") == "1"

    def assert_compatible(
        self, embedding_model: str, embedding_dim: int, embedding_provider: str = ""
    ) -> None:
        assert_embedding_compatible(
            self.read_conn, embedding_model, embedding_dim, embedding_provider
        )

    # ------------------------------------------------------------------ 写入事务

    def write(self):
        """写事务上下文：`with db.write() as conn:`，异常回滚，正常提交。"""
        return _WriteTxn(self)

    def execute_write(self, sql: str, params: tuple = ()) -> None:
        with self.write() as conn:
            conn.execute(sql, params)

    @property
    def size_bytes(self) -> int:
        """主文件大小。

        刻意不含 WAL 边车文件：这个项目的卖点是"一个文件 = 一份记忆"，
        把 WAL 混进来会让两个库的大小看起来差不多（WAL 会涨到自动检查点的
        阈值附近，与库里实际有多少数据无关），从而失去可比性。
        WAL 大小单独用 `wal_bytes` 报告。
        """
        return self.path.stat().st_size if self.path.exists() else 0

    @property
    def wal_bytes(self) -> int:
        total = 0
        for suffix in ("-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total


class _WriteTxn:
    def __init__(self, db: Database) -> None:
        self._db = db

    def __enter__(self) -> sqlite3.Connection:
        self._db._write_lock.acquire()
        return self._db.write_conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        conn = self._db.write_conn
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        finally:
            self._db._write_lock.release()
        return False


class Library:
    """一组已打开的库，按 display_name 索引。"""

    def __init__(self, wal: bool = True) -> None:
        self._dbs: dict[str, Database] = {}
        self._lock = threading.Lock()
        self.wal = wal

    def add(self, display_name: str, db: Database) -> None:
        with self._lock:
            self._dbs[display_name] = db

    def get(self, display_name: str | None = None) -> Database:
        with self._lock:
            if display_name is None:
                if len(self._dbs) == 1:
                    return next(iter(self._dbs.values()))
                raise KeyError("存在多个库，必须指定库名")
            if display_name not in self._dbs:
                raise KeyError(f"未挂载的库：{display_name}")
            return self._dbs[display_name]

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._dbs)

    def close(self) -> None:
        with self._lock:
            for db in self._dbs.values():
                db.close()
            self._dbs.clear()


__all__ = ["Database", "EmbeddingMismatchError", "Library"]
