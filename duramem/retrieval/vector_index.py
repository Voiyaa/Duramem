"""向量索引。

收敛在 `VectorIndex` 接口之后：当前实现是 sqlite-vec（暴力 KNN，无 ANN）。
规模上限见设计文档 §3.8；超限或 sqlite-vec 的 alpha 状态出问题时，
替换成 zvec / Milvus 只需再写一个实现类，不动调用方。
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from typing import Protocol

from duramem.store.database import Database
from duramem.store.schema import META_VECTOR_SOURCE, META_VECTORS_STALE, update_meta


class VectorIndex(Protocol):
    dim: int

    def add(self, chunk_id: int, vector: list[float]) -> None: ...
    def add_many(self, items: Iterable[tuple[int, list[float]]]) -> None: ...
    def remove(self, chunk_id: int) -> None: ...
    def search(self, vector: list[float], k: int) -> list[tuple[int, float]]: ...
    def rebuild(self, items: Iterable[tuple[int, list[float]]]) -> int: ...
    def count(self) -> int: ...
    def clear(self) -> None: ...


class SqliteVecIndex:
    """sqlite-vec 实现。写入走写连接，检索走读连接。"""

    name = "sqlite-vec"

    def __init__(
        self,
        db: Database,
        dim: int,
        source: str = "",
        table: str = "chunks_vec",
        id_column: str = "chunk_id",
        stamp_provenance: bool = True,
    ) -> None:
        if dim <= 0:
            raise ValueError(f"向量维度必须为正数，收到 {dim}")
        self.db = db
        self.dim = dim
        # 写入向量的那一刻顺手记下"是谁生成的"。库内没有来源且还是空的，
        # 说明这批向量就是当前提供方写进去的——此时记录是无歧义的；
        # 已有向量的库不在这里猜，那是配置变更时的责任（见 mark_vectors_stale）。
        self.source = source
        # 表名可换，让会话层的向量表复用这个实现（同一套 sqlite-vec 语义，
        # 独立 id 空间）。默认值就是切片表，现有调用方不受影响。
        self.table = table
        self.id_column = id_column
        # 来源指纹是**库级**事实，由切片索引负责记录。会话索引传 False：
        # 它按另一张表的 emptiness 判断"要不要盖章"会得出错误结论。
        self.stamp_provenance = stamp_provenance
        self._stamped = bool(db.vector_source)

    # ------------------------------------------------------------------

    def _stamp_provenance(self) -> None:
        """首次写入向量时记录来源指纹。只写一次，之后走内存标记。"""
        if self._stamped or not self.source or not self.stamp_provenance:
            return
        if self.db.vector_source or self.count() > 0:
            self._stamped = True
            return
        with self.db.write() as conn:
            update_meta(conn, **{META_VECTOR_SOURCE: self.source, META_VECTORS_STALE: "0"})
        self._stamped = True

    def _pack(self, vector: list[float]) -> bytes:
        if len(vector) != self.dim:
            raise ValueError(f"向量维度不符：期望 {self.dim}，收到 {len(vector)}")
        return struct.pack(f"{self.dim}f", *vector)

    def add(self, chunk_id: int, vector: list[float]) -> None:
        """写入或更新一条向量。

        注意：vec0 虚拟表**不支持** `INSERT OR REPLACE`——主键冲突时会直接抛
        UNIQUE constraint failed，于是"编辑切片后重算向量"会静默失败、一直用着旧向量。
        所以这里显式先删后插。
        """
        packed = self._pack(vector)
        self._stamp_provenance()
        with self.db.write() as conn:
            conn.execute(
                f"DELETE FROM {self.table} WHERE {self.id_column} = ?", (chunk_id,)
            )
            conn.execute(
                f"INSERT INTO {self.table}({self.id_column}, embedding) VALUES (?, ?)",
                (chunk_id, packed),
            )

    def add_many(self, items: Iterable[tuple[int, list[float]]]) -> None:
        rows = [(cid, self._pack(vec)) for cid, vec in items]
        if not rows:
            return
        self._stamp_provenance()
        with self.db.write() as conn:
            conn.executemany(
                f"DELETE FROM {self.table} WHERE {self.id_column} = ?",
                [(cid,) for cid, _ in rows],
            )
            conn.executemany(
                f"INSERT INTO {self.table}({self.id_column}, embedding) VALUES (?, ?)",
                rows,
            )

    def remove(self, chunk_id: int) -> None:
        with self.db.write() as conn:
            conn.execute(
                f"DELETE FROM {self.table} WHERE {self.id_column} = ?", (chunk_id,)
            )

    def search(self, vector: list[float], k: int) -> list[tuple[int, float]]:
        if k <= 0:
            return []
        rows = self.db.read_conn.execute(
            f"SELECT {self.id_column}, distance FROM {self.table} "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (self._pack(vector), k),
        ).fetchall()
        return [(int(r[self.id_column]), float(r["distance"])) for r in rows]

    def rebuild(self, items: Iterable[tuple[int, list[float]]]) -> int:
        self.clear()
        rows = [(cid, self._pack(vec)) for cid, vec in items]
        if not rows:
            return 0
        self._stamp_provenance()
        with self.db.write() as conn:
            conn.executemany(
                f"INSERT INTO {self.table}({self.id_column}, embedding) VALUES (?, ?)",
                rows,
            )
        return len(rows)

    def count(self) -> int:
        return int(
            self.db.read_conn.execute(f"SELECT count(*) FROM {self.table}").fetchone()[0]
        )

    def clear(self) -> None:
        with self.db.write() as conn:
            conn.execute(f"DELETE FROM {self.table}")


__all__ = ["SqliteVecIndex", "VectorIndex"]
