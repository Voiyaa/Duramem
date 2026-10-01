"""多库注册表。

偏离设计文档 v2 的一处：库文件名不再用 `<db_uuid>.db`，而是用**可读名**派生
（`data/work.db`、`data/工作.db`）。理由是"一个文件 = 一份记忆"要能直接拷走辨认，
而身份由文件内部的 `db_meta.db_uuid` 保证——即使文件被改名、搬移、复制，
身份仍然稳定。registry 只负责"显示名 → 文件路径"的映射。
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from duramem.config import Settings
from duramem.store.database import Database, Library

REGISTRY_VERSION = 1

# 类里有同名方法 `list`：类体内的 `list[...]` 注解会被 mypy 解析成那个方法
# （valid-type）。注解统一走这个别名，公共方法名保持不变。
StrList = list[str]

_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def new_uuid() -> str:
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    """把显示名转成安全的文件名主干。保留中文，替换非法字符。"""
    cleaned = _ILLEGAL_FILENAME.sub("_", name.strip())
    cleaned = cleaned.strip(" .")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "db"


@dataclass
class RegistryEntry:
    display_name: str
    file_path: str
    db_uuid: str
    created_at: str

    def to_json(self) -> dict[str, str]:
        return {
            "display_name": self.display_name,
            "file_path": self.file_path,
            "db_uuid": self.db_uuid,
            "created_at": self.created_at,
        }


class DatabaseRegistry:
    """`data/registry.json` 的读写与库生命周期管理。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "registry.json"
        self._lock = threading.RLock()
        self._entries: dict[str, RegistryEntry] = {}

    # ------------------------------------------------------------------ 读写

    def load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if not self.path.exists():
                self._entries = {}
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries: dict[str, RegistryEntry] = {}
            for name, item in (raw.get("databases") or {}).items():
                entries[name] = RegistryEntry(
                    display_name=name,
                    file_path=item["file_path"],
                    db_uuid=item.get("db_uuid", ""),
                    created_at=item.get("created_at", ""),
                )
            self._entries = entries

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": REGISTRY_VERSION,
                "databases": {n: e.to_json() for n, e in self._entries.items()},
            }
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    # ------------------------------------------------------------------ 查询

    def list(self) -> list[RegistryEntry]:
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: e.created_at)

    def get(self, display_name: str) -> RegistryEntry:
        with self._lock:
            if display_name not in self._entries:
                raise KeyError(
                    f"库不存在：{display_name}（已有：{', '.join(sorted(self._entries)) or '无'}）"
                )
            return self._entries[display_name]

    def exists(self, display_name: str) -> bool:
        with self._lock:
            return display_name in self._entries

    # ------------------------------------------------------------------ 生命周期

    def create(
        self,
        display_name: str,
        settings: Settings,
        reranker_model: str = "",
    ) -> Database:
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("库名不能为空")
        if self.exists(display_name):
            raise ValueError(f"库已存在：{display_name}")

        db_uuid = new_uuid()
        file_path = self.data_dir / f"{slugify(display_name)}.db"
        if file_path.exists():
            # 文件名撞车但显示名不同：附上 uuid 后缀区分
            file_path = self.data_dir / f"{slugify(display_name)}.{db_uuid}.db"

        db = Database(file_path, wal=settings.sqlite_wal)
        db.initialize(
            db_uuid=db_uuid,
            created_at=now_iso(),
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
            summary_soft_limit_tokens=settings.summary_soft_limit_tokens,
            reranker_model=reranker_model,
            vector_chunk_size=settings.vector_chunk_size,
        )

        entry = RegistryEntry(
            display_name=display_name,
            file_path=str(file_path),
            db_uuid=db_uuid,
            created_at=now_iso(),
        )
        with self._lock:
            self._entries[display_name] = entry
        self.save()
        return db

    def rename(self, old: str, new: str, rename_file: bool = False) -> RegistryEntry:
        new = new.strip()
        if not new:
            raise ValueError("新库名不能为空")
        entry = self.get(old)
        if new != old and self.exists(new):
            raise ValueError(f"库名已被占用：{new}")

        old_path = Path(entry.file_path)
        if rename_file and old_path.exists():
            target = self.data_dir / f"{slugify(new)}.db"
            if target != old_path and not target.exists():
                old_path.rename(target)
                entry.file_path = str(target)

        with self._lock:
            self._entries.pop(old, None)
            entry.display_name = new
            self._entries[new] = entry
        self.save()
        return entry

    def repoint(self, display_name: str, file_path: str | Path) -> RegistryEntry:
        """把库指向另一个文件（库文件被手工搬移后使用）。"""
        entry = self.get(display_name)
        entry.file_path = str(Path(file_path))
        self.save()
        return entry

    def remove(self, display_name: str, purge_file: bool = False) -> StrList:
        """从注册表移除库；`purge_file=True` 时连库文件一起删。

        返回**没删掉的**文件路径（空列表 = 全删干净了）。之所以要返回而不是
        静默了事：Windows 上别的进程（MCP 子进程、serve、hook）打开着库文件时
        是删不掉的，而注册表条目已经没了——此时若只报"已删除"，用户会以为删干净了，
        下次 `discover()` 扫描数据目录还会把这个文件重新收养回来，看起来像"删了又回来"。
        """
        entry = self.get(display_name)
        with self._lock:
            self._entries.pop(display_name, None)
        leftover: list[str] = []
        if purge_file:
            leftover = self._purge_files(self.resolve_path(entry))
        self.save()
        return leftover

    @staticmethod
    def _purge_files(path: Path) -> StrList:
        """删除库文件及其 WAL 边车文件，返回删不掉的那些。

        Windows 上文件句柄释放有延迟，直接 unlink 会偶发 PermissionError；
        重试几次即可。仍删不掉的**如实返回**给调用方，由它去告诉用户。
        """
        import gc
        import time

        for attempt in range(5):
            pending = [
                candidate
                for suffix in ("-wal", "-shm", "")
                if (candidate := Path(str(path) + suffix)).exists()
            ]
            if not pending:
                return []
            for candidate in pending:
                try:
                    candidate.unlink()
                except OSError:
                    pass
            gc.collect()
            time.sleep(0.1 * (attempt + 1))

        return [
            str(candidate)
            for suffix in ("", "-wal", "-shm")
            if (candidate := Path(str(path) + suffix)).exists()
        ]

    def discover(self) -> StrList:
        """扫描数据目录，收养未被登记的 .db 文件（从文件内读取 db_uuid）。

        处理"用户直接把一个库文件拷进来"的场景——这是"一个文件 = 一份记忆"的
        自然延伸，不该要求用户手工登记。

        两条约束都关于**同一份记忆只能有一个身份**：

        - 比对已登记集合时用 `resolve_path`，否则一条相对路径登记会把数据目录里
          同一个文件认成"新库"，收养出一条 `work-2`（实测出现过：库文件原地不动，
          却因为相对路径解析到别处而多出一个重复条目）。
        - 名字已被占用时，先看是不是"同一个库换了位置"——`db_uuid` 相同而旧路径
          已不存在，那就改指向，而不是另起一个 `<名字>-2`。
        """
        adopted: list[str] = []
        known_files = {str(self.resolve_path(e).resolve()) for e in self.list()}

        for candidate in sorted(self.data_dir.glob("*.db")):
            if str(candidate.resolve()) in known_files:
                continue
            try:
                probe = Database(candidate, wal=False)
                meta = probe.meta
                if not meta.get("db_uuid"):
                    probe.close()
                    continue
                db_uuid = meta["db_uuid"]
                created_at = meta.get("created_at", now_iso())
                probe.close()
            except Exception:
                continue

            moved = next(
                (
                    e
                    for e in self.list()
                    if e.db_uuid
                    and e.db_uuid == db_uuid
                    and not self.resolve_path(e).exists()
                ),
                None,
            )
            if moved is not None:
                self.repoint(moved.display_name, candidate)
                adopted.append(moved.display_name)
                continue

            name = candidate.stem
            base = name
            index = 2
            while self.exists(name):
                name = f"{base}-{index}"
                index += 1

            with self._lock:
                self._entries[name] = RegistryEntry(
                    display_name=name,
                    file_path=str(candidate),
                    db_uuid=db_uuid,
                    created_at=created_at,
                )
            adopted.append(name)

        if adopted:
            self.save()
        return adopted

    # ------------------------------------------------------------------ 打开

    def resolve_path(self, entry: RegistryEntry) -> Path:
        """把登记里的 `file_path` 解析成可用的绝对路径。

        新登记的路径本来就是绝对的（`Settings.__post_init__` 已经把 data_dir 绝对化），
        这一层是为了兜住历史遗留的相对路径——它当年是按**建库时的 cwd** 存下来的
        （典型值 `data\\work.db`，相对项目根而非数据目录），所以候选顺序是
        「原样 → 数据目录 → 数据目录的上一级」。

        为什么必须有这一层：MCP 子进程与 hook 的 cwd 由宿主决定（通常是用户当时
        打开的项目目录），照 cwd 解析相对路径会指向不存在的位置，库根本打不开。
        """
        raw = Path(entry.file_path)
        if raw.is_absolute():
            return raw
        for base in (self.data_dir, self.data_dir.parent):
            candidate = base / raw
            if candidate.exists():
                return candidate
        return self.data_dir / raw

    def _open(self, path, settings: Settings) -> Database:
        """打开一个库并把结构升到当前版本。

        迁移必须在这里发生：`Database.initialize` 只在**建库**时被调用，而存量库
        走的是打开路径。最初把迁移挂在 `init_database` 上，结果存量库打开后仍是
        旧列名——实测才发现。见 `schema.ensure_schema`。
        """
        db = Database(path, wal=settings.sqlite_wal)
        db.ensure_schema(
            fallback_dim=settings.embedding_dim,
            fallback_chunk_size=settings.vector_chunk_size,
        )
        return db

    def open_library(self, settings: Settings) -> Library:
        """打开全部已登记的库，返回 Library。"""
        library = Library(wal=settings.sqlite_wal)
        for entry in self.list():
            path = self.resolve_path(entry)
            if not path.exists():
                continue
            library.add(entry.display_name, self._open(path, settings))
        return library

    def open_one(self, display_name: str, settings: Settings) -> Database:
        entry = self.get(display_name)
        return self._open(self.resolve_path(entry), settings)


def get_registry(settings: Settings) -> DatabaseRegistry:
    registry = DatabaseRegistry(settings.data_dir)
    registry.load()
    return registry
