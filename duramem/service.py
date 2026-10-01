"""服务层：跨库调度与对外能力。

MCP 与 FastAPI 共用这一层，保证两条入口的行为完全一致——
不存在"MCP 能做的事前端做不到"或反过来的情况。

uid 寻址规则（设计文档 §8）：
- 传了 db → 直接在指定库查
- 没传 db → 按挂载顺序在各库中查找首个命中
跨库检索的命中结果会带 `db` 字段，模型据此把 db 传回来。

低耦合的领域已拆出（A.15，Service 保留同名门面转发，调用面不变）：
历史导入/库快照/归档往返 → `importing.py`；模型端点连通性探测 →
`providers/connectivity.py`。往 service.py 加新能力前先想想它属于哪个域。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from duramem import active_db
from duramem.active_db import ACTIVE_FILE as _ACTIVE_FILE  # noqa: F401  (对外导出用)
from duramem.config import Settings, get_settings
from duramem.errors import DuramemError
from duramem.importing import ImportExportDomain
from duramem.models import ChunkDraft, Message
from duramem.provider_config import PROVIDER_FIELD_NAMES, VECTOR_IDENTITY_FIELDS, ProviderConfig
from duramem.providers.connectivity import probe_embedding, probe_rerank, probe_summary
from duramem.providers.embedding import build_embedding_provider
from duramem.providers.presets import presets_for
from duramem.providers.rerank import build_reranker
from duramem.reader import (
    SESSION_DETAIL_OVERVIEW,
    OriginalReader,
    ReadResult,
    ReadUsageTracker,
    SessionReadResult,
)
from duramem.retrieval.pipeline import RetrievalPipeline, RetrievalResult
from duramem.retrieval.sessions import SessionRetriever
from duramem.retrieval.vector_index import SqliteVecIndex
from duramem.runtime_settings import RUNTIME_TUNABLE, RuntimeSettings
from duramem.session_summarizer import (
    REFRESH_NOW,
    SessionSummarizer,
    SessionSummaryOutcome,
    build_overview_provider,
)
from duramem.store.database import Database, EmbeddingMismatchError, Library
from duramem.store.registry import DatabaseRegistry
from duramem.store.repository import Repository, content_hash
from duramem.store.session_layers import SessionLayerStore
from duramem.summarizer import Summarizer, SummaryOutcome
from duramem.text.tokenize import Segmenter, get_segmenter


@dataclass
class _Components:
    """一个库的全部运行时组件。"""

    name: str
    db: Database
    repo: Repository
    index: SqliteVecIndex
    session_index: SqliteVecIndex
    session_retriever: SessionRetriever
    pipeline: RetrievalPipeline
    reader: OriginalReader
    summarizer: Summarizer
    session_layers: SessionLayerStore
    session_summarizer: SessionSummarizer


class Service:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        # 跨进程热重载的基线：.env 默认值快照（必须在任何 apply 之前抓）。
        # 界面进程删掉某个覆盖项时，本进程要先打回 .env 默认再套剩余覆盖值，
        # 否则被删的项会在自己内存里残留旧值。两个白名单有交集
        # （rerank_enabled），所以分开存两份。
        self._env_tunable_defaults = {
            name: getattr(self.settings, name)
            for name in RUNTIME_TUNABLE
            if hasattr(self.settings, name)
        }
        self._env_provider_defaults = {
            name: getattr(self.settings, name)
            for name in PROVIDER_FIELD_NAMES
            if hasattr(self.settings, name)
        }
        # .env 是启动默认值，运行时覆盖值在它之上生效。
        # 必须在构造 ReadUsageTracker / 组件之前应用，否则它们会捕获旧值。
        self.runtime = RuntimeSettings(self.settings)
        self.runtime.load()
        self.runtime.apply()

        # 模型配置在运行时覆盖之后应用：交集只有 rerank_enabled，
        # 模型配置是"更晚写的、更具体的"，放后面更符合直觉。
        self.providers = ProviderConfig(self.settings)
        self.providers.load()
        self.providers.apply()

        # 界面（另一个进程）改配置文件的检测基线：文件指纹，变了才重载
        self._runtime_stamp = self._file_stamp(self.runtime.path)
        self._provider_stamp = self._file_stamp(self.providers.path)

        self.segmenter: Segmenter = get_segmenter(
            extra_words=self.settings.tokenizer_extra_words,
            user_dict=self.settings.tokenizer_user_dict or None,
            stopwords_enabled=self.settings.tokenizer_stopwords_enabled,
        )
        self.registry: DatabaseRegistry = self._load_registry()
        self.library = Library(wal=self.settings.sqlite_wal)
        self.usage = ReadUsageTracker(
            self.settings.read_soft_limit_tokens, self.settings.read_budget_idle_reset
        )
        self._cache: dict[str, _Components] = {}
        # 当前记忆库指针的 mtime 缓存：(文件指纹, 解析结果)
        self._active_cache: tuple[Any, dict[str, Any]] | None = None
        self._lock = threading.RLock()
        # 导入/归档域（历史导入、快照、归档往返）住在 importing.py；这里组合它并
        # 保留同名门面方法，调用面（api.py / mcp_server.py / CLI）不变。见 A.15。
        self._importing = ImportExportDomain(self)

    # ================================================================== 初始化

    def _load_registry(self) -> DatabaseRegistry:
        """加载注册表，并收养数据目录里未被登记的 .db 文件。

        收养是为了支持"把一个库文件拷进来就能用"——这是"一个文件 = 一份记忆"
        的自然延伸，不该要求用户手工登记。
        """
        registry = DatabaseRegistry(self.settings.data_dir)
        registry.load()
        registry.discover()
        return registry

    def active_view(self) -> dict[str, Any]:
        """当前记忆库指针（前端选定的那个库）。

        按 mtime 缓存：MCP 每次工具调用都要问一遍"现在是哪个库"，每次都读盘没必要，
        但文件一变必须立刻看到——热切换就靠这个。
        """
        path = active_db.path_for(self.settings.data_dir)
        try:
            stamp: tuple[int, int] | None = (path.stat().st_mtime_ns, path.stat().st_size)
        except OSError:
            stamp = None
        with self._lock:
            cached = self._active_cache
            if cached is not None and cached[0] == stamp:
                return cached[1]
        info = active_db.read(self.settings.data_dir)
        info["source"] = "active_db.json" if info["db"] else "none"
        info["exists"] = bool(info["db"]) and self.registry.exists(str(info["db"]))
        with self._lock:
            self._active_cache = (stamp, info)
        return info

    def set_active_db(self, name: str, by: str = "ui") -> dict[str, Any]:
        """选定当前记忆库。只允许选存在的库——不存在的库选了也没意义。"""
        if not self.registry.exists(name):
            available = ", ".join(e.display_name for e in self.registry.list()) or "无"
            raise DuramemError(f"库不存在：{name}；可用：{available}")
        payload = active_db.write(self.settings.data_dir, name, by=by)
        with self._lock:
            self._active_cache = None
        return payload

    def ensure_default_db(self) -> str:
        """当前记忆库：显式选定 > `--db` > 注册表第一条；注册表为空才隐式建库。

        指针指向一个**不存在的库**时不静默兜底——那正是"看起来正常、其实查错库"
        的来源（实测踩过：注册表第一条恰好是别的库，`dm_stats` 就报成了它的数字）。
        宁可报清楚错，让用户去界面上重新选。
        """
        info = self.active_view()
        if info["db"]:
            if info["exists"]:
                return str(info["db"])
            raise DuramemError(
                f"当前记忆库「{info['db']}」已不存在"
                f"（来自 {info.get('source', 'active_db.json')}，"
                f"更新于 {info.get('updated_at') or '未知时间'}）。"
                "请在界面上重新选定一个库；可用："
                + (", ".join(e.display_name for e in self.registry.list()) or "无")
            )

        preferred = (self.settings.default_db or "").strip()
        if preferred and self.registry.exists(preferred):
            return preferred

        entries = self.registry.list()
        if entries:
            return entries[0].display_name

        name = self.settings.default_db
        self.registry.create(name, self.settings)
        self.set_active_db(name, by="auto-init")
        return name

    def _components(self, name: str, verify: bool = True) -> _Components:
        """取（或构造）一个库的运行时组件。

        `verify=False` 跳过向量模型一致性校验。**只有** `rebuild_vectors`
        该用它：那条路径的目的正是修正不一致，而它必须在"指纹已经对不上"的
        状态下先把向量算出来，算成功了才更新指纹。反过来（先更新指纹再算）
        会把库留在一个说谎的状态：元数据说"一致"，实际向量已经被清空了。
        """
        # 先看界面进程有没有改配置文件（见 _maybe_hot_reload）。文件没变时
        # 这只是一次 stat()；变了会就地作废缓存，下面的命中检查自然失效
        self._maybe_hot_reload()
        with self._lock:
            if name in self._cache:
                return self._cache[name]

        try:
            db = self.registry.open_one(name, self.settings)
        except KeyError as exc:
            # 统一成 DuramemError，让 REST / MCP 的既有处理分支都能覆盖
            raise DuramemError(str(exc)) from exc

        # 向量模型一致性校验：不一致直接报错，不静默返回噪声结果。
        # 传"实际生效"的提供方而不只是模型名——没填 Key 时实际用的是离线哈希，
        # 只比模型名会让"配置 bge-m3 + 库里其实是哈希向量"这种组合蒙混过关。
        if verify:
            db.assert_compatible(
                self.settings.embedding_model,
                self.settings.embedding_dim,
                self.providers.effective_embedding()["provider"],
            )

        repo = Repository(db, self.segmenter)
        embedder = build_embedding_provider(
            api_key=self.settings.embedding_api_key,
            base_url=self.settings.embedding_base_url,
            model=self.settings.embedding_model,
            dim=self.settings.embedding_dim,
            batch_size=self.settings.embed_batch_size,
            force_offline=self.settings.embedding_fake,
            segmenter=self.segmenter,
            send_dimensions=self.settings.embedding_send_dimensions,
        )
        reranker = build_reranker(
            enabled=self.settings.rerank_enabled,
            api_key=self.settings.rerank_api_key,
            base_url=self.settings.rerank_base_url,
            model=self.settings.rerank_model,
            segmenter=self.segmenter,
        )
        index = SqliteVecIndex(
            db,
            dim=int(db.embedding_dim or self.settings.embedding_dim),
            source=self.providers.effective_embedding()["provider"],
        )
        session_layers = SessionLayerStore(db, self.segmenter)
        # 会话向量表独立 id 空间，且**不负责**盖来源指纹——那是库级事实，
        # 由切片索引记录（见 SqliteVecIndex 的 stamp_provenance）。
        session_index = SqliteVecIndex(
            db,
            dim=int(db.embedding_dim or self.settings.embedding_dim),
            source=self.providers.effective_embedding()["provider"],
            table="session_layers_vec",
            id_column="layer_id",
            stamp_provenance=False,
        )
        session_retriever = SessionRetriever(
            session_layers,
            embedder,
            reranker,
            session_index,
            self.settings,
            db_name=name,
        )
        pipeline = RetrievalPipeline(
            repo,
            embedder,
            reranker,
            index,
            self.settings,
            db_name=name,
            session_retriever=session_retriever,
        )
        reader = OriginalReader(
            repo, self.settings, self.usage, db_name=name, session_layers=session_layers
        )
        summarizer = Summarizer(repo, embedder, index, self.settings)
        session_summarizer = SessionSummarizer(
            session_layers,
            build_overview_provider(self.settings, self.segmenter),
            self.settings,
            embedder,
            session_index,
        )

        components = _Components(
            name,
            db,
            repo,
            index,
            session_index,
            session_retriever,
            pipeline,
            reader,
            summarizer,
            session_layers,
            session_summarizer,
        )
        with self._lock:
            self._cache[name] = components
        return components

    def close(self) -> None:
        with self._lock:
            for components in self._cache.values():
                # 命中计数是缓冲批刷的（见 Repository.bump_hits），关库前落盘，
                # 否则干净退出也丢最近一个窗口的计数
                try:
                    components.repo.flush_hits()
                except Exception:  # noqa: BLE001 —— 展示数据，别挡住关库
                    pass
                components.db.close()
            self._cache.clear()

    def _close_cached(self, name: str) -> None:
        """关掉并移除某库的缓存组件。

        组件只在构造那一刻做一致性校验，之后配置或库指纹变了也不会重验——
        所以任何改变库身份状态的操作（删除、重建向量、标记向量作废）都必须
        先把缓存里的它关掉，否则后续调用命中缓存、绕过校验。
        """
        with self._lock:
            stale = self._cache.pop(name, None)
        if stale is not None:
            stale.db.close()

    # ================================================================== 库管理

    def list_databases(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        active = self.active_view()
        for entry in self.registry.list():
            info: dict[str, Any] = {
                "name": entry.display_name,
                "file_path": entry.file_path,
                "db_uuid": entry.db_uuid,
                "created_at": entry.created_at,
                # 当前记忆库（前端选定、MCP 跟随）。界面据此打徽章。
                "active": entry.display_name == active.get("db"),
            }
            try:
                components = self._components(entry.display_name)
                stats = components.repo.stats()
                info.update(
                    {
                        "chunks_alive": stats["chunks_alive"],
                        "messages": stats["messages"],
                        "vectors": stats["vectors"],
                        "chunks_missing_vectors": stats["chunks_missing_vectors"],
                        "size_bytes": stats["size_bytes"],
                        "wal_bytes": stats["wal_bytes"],
                        "embedding_model": stats["meta"].get("embedding_model"),
                        # db_meta 是键值 TEXT 表，这里转回 int，避免前端拿到 "1024"
                        "embedding_dim": int(stats["meta"].get("embedding_dim") or 0),
                        "vector_source": stats.get("vector_source") or "",
                        "vectors_stale": bool(stats.get("vectors_stale")),
                        "compatible": True,
                        # 每库自己的冷启动注入配置（库管理页编辑）
                        "cold_start": self._cold_start_from_meta(stats["meta"]),
                    }
                )
            except EmbeddingMismatchError as exc:
                info["compatible"] = False
                info["error"] = str(exc)
                # 一致性校验拦下来的库，仍然要能看见它的向量是谁生成的——
                # 否则界面上只会显示"不一致"，用户不知道该重建还是该改配置。
                info.update(self._read_vector_state(entry.file_path))
            except Exception as exc:  # noqa: BLE001
                info["compatible"] = False
                info["error"] = _describe_open_failure(exc)
            out.append(info)
        return out

    def _read_vector_state(self, file_path: str) -> dict[str, Any]:
        """只读 meta，用于在不通过一致性校验的情况下仍然报出向量来源。"""
        try:
            db = Database(Path(file_path), wal=self.settings.sqlite_wal)
            try:
                return {
                    "vector_source": db.vector_source,
                    "vectors_stale": db.vectors_stale,
                    "embedding_dim": db.embedding_dim,
                    "chunks_alive": db.count_chunks(),
                }
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            return {}

    # ================================================================ 冷启动注入

    # db_meta 键。冷启动配置是**库级属性**：每个库有自己的开局说明，绑定随
    # 文件走（复制/快照/归档往返都带着），不进全局 runtime_settings。
    COLD_START_NOTE_KEY = "cold_start_note"
    COLD_START_ENABLED_KEY = "cold_start_enabled"

    @staticmethod
    def _cold_start_from_meta(meta: dict[str, str]) -> dict[str, Any]:
        return {
            "note": str(meta.get(Service.COLD_START_NOTE_KEY) or ""),
            # 键缺失视为开（默认）：空 note 才是真正的关闭条件
            "enabled": str(meta.get(Service.COLD_START_ENABLED_KEY) or "1") != "0",
        }

    def cold_start_view(self, db: str | None = None) -> dict[str, Any]:
        """某库的冷启动注入配置。MCP 注入器每次工具调用都从这里取（读 meta，即改即生效）。"""
        name = db or self.ensure_default_db()
        components = self._components(name)
        return {"db": name, **self._cold_start_from_meta(components.db.meta)}

    def set_cold_start(
        self,
        db: str | None = None,
        note: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """写某库的冷启动注入配置。note/enabled 都传 None 表示不改动该项。

        改动本身就会让 MCP 侧的边沿状态重新武装（注入器记的是"上次见到的
        配置"，变了就算一次新边沿）。
        """
        name = db or self.ensure_default_db()
        components = self._components(name)
        values: dict[str, str] = {}
        if note is not None:
            values[self.COLD_START_NOTE_KEY] = str(note)
        if enabled is not None:
            values[self.COLD_START_ENABLED_KEY] = "1" if enabled else "0"
        if values:
            components.db.update_meta(**values)
        return {"db": name, **self._cold_start_from_meta(components.db.meta)}

    def last_mcp_call(self) -> dict[str, Any]:
        """最近一次 MCP 工具调用的输入/输出（MCP 子进程写的 mcp_last_call.json）。

        读失败按不存在处理：这份文件是诊断快照，不是要保真的数据。
        """
        path = Path(self.settings.data_dir) / "mcp_last_call.json"
        if not path.exists():
            return {"exists": False}
        try:
            return {"exists": True, **json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError) as exc:
            return {"exists": False, "error": str(exc)}

    # ================================================================== 统计

    # 摘要触顶占比的告警线：超过它才作为异常信号出现在 stats_brief 里
    # （与前端旧提示文案同源："超过 20% 说明摘要上限该放宽"）。
    SUMMARY_TRUNCATED_SIGNAL_RATIO = 0.2

    def stats_brief(self, stats: dict[str, Any], active_info: dict[str, Any]) -> dict[str, Any]:
        """统计的**精简投影**——前端状态卡与模型 `dm_stats` 看到的同一份事实。

        存在的理由：`stats()` 是给前端管理页的全量体检（文件/WAL 大小、索引
        后端、逐会话游标……），而模型的工具结果里那些字段既用不上也无法行动，
        纯上下文污染；两边各说各话又会让人看到的和模型拿到的是两张图。所以
        投影收在服务层做**一次**，两个消费者共用。原则：只留模型用得上、能
        行动的字段；异常信号用**条件字段**表达——出现即值得注意，不出现不占
        一个字节。

        `stats` / `active_info` 传调用方已取好的 `svc.stats()` 与
        `svc.active_view()`，避免为投影重复跑一遍统计。
        """
        cursors = stats.get("cursors", [])
        brief: dict[str, Any] = {
            "db": stats.get("db"),
            "active_db": active_info.get("db"),
            "chunks_alive": stats.get("chunks_alive"),
            "vectors": stats.get("vectors"),
            "messages": stats.get("messages"),
            "sessions_summarized": len(cursors),
        }
        times = [c.get("updated_at") for c in cursors if c.get("updated_at")]
        if times:
            brief["latest_summary_at"] = max(times)
        if stats.get("vectors_stale"):
            brief["vectors_stale"] = True
        if stats.get("chunks_missing_vectors"):
            brief["chunks_missing_vectors"] = stats["chunks_missing_vectors"]
        truncated = int(stats.get("summary_truncated") or 0)
        alive = int(stats.get("chunks_alive") or 0)
        if alive > 0 and truncated / alive > self.SUMMARY_TRUNCATED_SIGNAL_RATIO:
            brief["summary_truncated_ratio"] = round(truncated / alive, 2)
        warnings = [*stats.get("warnings", [])]
        if active_info.get("warning"):
            warnings.append(str(active_info["warning"]))
        if warnings:
            brief["warnings"] = warnings
        return brief

    def create_database(self, name: str) -> dict[str, Any]:
        self.registry.create(name, self.settings, reranker_model=self.settings.rerank_model)
        payload: dict[str, Any] = {"ok": True, "name": name}
        # 还没有"当前记忆库"时，把新建的设为当前——否则第一个库建完仍然没有"当前"，
        # 每次调用都得走 fallback，用户也看不出系统到底在用哪个库。
        info = self.active_view()
        if not info["db"] or not info["exists"]:
            self.set_active_db(name, by="auto-first")
            payload["active_set"] = True
        return payload

    def rename_database(self, old: str, new: str, rename_file: bool = False) -> dict[str, Any]:
        with self._lock:
            self._cache.pop(old, None)
        entry = self.registry.rename(old, new, rename_file=rename_file)
        payload: dict[str, Any] = {
            "ok": True,
            "name": entry.display_name,
            "file_path": entry.file_path,
        }
        # 指针存的是**显示名**，所以改名必须把指针一起改——否则指针记着一个不存在的
        # 名字，之后所有不带 db 的调用、以及界面里那条路径全部报"当前记忆库 X 已不存在"。
        # 实测就是这么踩到的：把「测试」改名后指针留在旧名字上，导入功能整个不可用。
        info = self.active_view()
        if info["db"] == old:
            self.set_active_db(entry.display_name, by="auto-rename")
            payload["active_renamed_to"] = entry.display_name
        return payload

    def delete_database(self, name: str, purge_file: bool = False) -> dict[str, Any]:
        """移除库；`purge_file=True` 时连文件一起删（真删除）。

        先关掉本进程持有的连接再删文件——否则 Windows 上一定删不掉。
        文件是否真的删掉了如实回报：留给调用方提示用户"还有进程占着它"。

        删掉的若是**当前记忆库**，指针必须跟着改：否则模型那边每次调用都会撞上
        "当前记忆库 X 已不存在"，而用户只看到界面上少了一个库，完全不知道发生了什么。
        """
        self._close_cached(name)
        leftover = self.registry.remove(name, purge_file=purge_file)
        payload: dict[str, Any] = {
            "ok": True,
            "purged": purge_file,
            "file_removed": bool(purge_file) and not leftover,
        }
        if leftover:
            payload["leftover"] = leftover
            payload["warnings"] = [
                "库已从注册表移除，但文件没能删掉（正被其它进程打开）："
                + "、".join(leftover)
                + "。关掉占用它的进程（serve / MCP 子进程 / hook）后重试或手工删除；"
                "否则下次扫描数据目录会把它重新收养回来。"
            ]

        info = self.active_view()
        if info["db"] == name:
            remaining = [entry.display_name for entry in self.registry.list()]
            if remaining:
                self.set_active_db(remaining[0], by="auto-switch")
                payload["active_switched_to"] = remaining[0]
            else:
                active_db.clear(self.settings.data_dir)
                with self._lock:
                    self._active_cache = None
                payload["active_cleared"] = True
        return payload

    # ================================================================== 采集

    def ingest(
        self,
        messages: Sequence[Message],
        db: str | None = None,
        auto_summarize: bool = True,
    ) -> dict[str, Any]:
        """采集适配器的入口：写入原始消息，必要时触发总结。"""
        name = db or self.ensure_default_db()
        components = self._components(name)
        ids = components.repo.upsert_messages(list(messages))

        payload: dict[str, Any] = {
            "ok": True,
            "db": name,
            "accepted": len(ids),
            "message_ids": ids,
        }

        if auto_summarize and self.settings.auto_summary_enabled and messages:
            window_id = messages[0].window_id
            session_id = messages[0].session_id
            outcome = components.summarizer.maybe_auto_summarize(window_id, session_id)
            if outcome is not None:
                payload["auto_summary"] = outcome.to_payload()

        return payload

    def summarize(
        self,
        window_id: str,
        session_id: str,
        db: str | None = None,
        messages: Sequence[Message] | None = None,
    ) -> SummaryOutcome:
        name = db or self.ensure_default_db()
        components = self._components(name)
        return components.summarizer.summarize(
            window_id, session_id, messages=messages, source_db=components.db.db_uuid
        )

    # ================================================================ 会话层

    def summarize_session(
        self,
        window_id: str,
        session_id: str,
        db: str | None = None,
    ) -> SessionSummaryOutcome:
        """生成/重生成该会话的概览（L1）与派生的摘要（L0）。

        **强制生成**，不判断是否值得刷新——那个判断属于 freshness 策略。
        手动触发（界面按钮 / CLI `summarize-session`）走这条。
        """
        name = db or self.ensure_default_db()
        components = self._components(name)
        return components.session_summarizer.summarize(window_id, session_id)

    def get_session_layer(
        self, session_id: str, db: str | None = None, window_id: str | None = None
    ) -> dict[str, Any] | None:
        """取会话层。给 window_id 时按精确键查，否则按 session_id 取最近一份。

        后者才是 MCP 的常规入口：模型手里只有检索命中回传的 `session_id`，
        没有 window_id。
        """
        name = db or self.ensure_default_db()
        components = self._components(name)
        if window_id is not None:
            return components.session_layers.get(window_id, session_id)
        return components.session_layers.get_by_session(session_id)

    def read_session(
        self,
        session_id: str,
        detail: str = SESSION_DETAIL_OVERVIEW,
        db: str | None = None,
        session: str = "default",
        window_id: str | None = None,
    ) -> SessionReadResult:
        """读取会话层的 L0/L1。受 `overview_access_enabled` 开关与取回量计数约束。"""
        name = db or self.ensure_default_db()
        components = self._components(name)
        return components.reader.read_session(
            session_id, detail=detail, session=session, window_id=window_id
        )

    def list_session_layers(
        self, db: str | None = None, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        name = db or self.ensure_default_db()
        components = self._components(name)
        return components.session_layers.list(limit=limit, offset=offset)

    def refresh_sessions(
        self, db: str | None = None, limit: int = 0, force: bool = False
    ) -> dict[str, Any]:
        """按 freshness 策略批量刷新会话概览。

        刻意**不做成自动触发**：hook 是同步执行的（每轮已多约 0.9 秒），在采集
        路径上塞一次可能长达数十秒的模型调用会直接拖慢每一轮对话。所以它是显式
        动作——界面按钮或 CLI。

        `force=True` 绕过策略、全部重生成（用户点"重新生成概览"用）。
        """
        name = db or self.ensure_default_db()
        components = self._components(name)
        candidates = components.session_layers.refresh_candidates(limit=limit)

        results: list[dict[str, Any]] = []
        refreshed = 0
        for row in candidates:
            window_id, session_id = row["window_id"], row["session_id"]
            outcome: SessionSummaryOutcome | None
            if force:
                decision, reason = REFRESH_NOW, "强制重生成"
                outcome = components.session_summarizer.summarize(window_id, session_id)
            else:
                judged = components.session_summarizer.maybe_refresh(window_id, session_id)
                decision, reason = judged.decision, judged.reason
                outcome = judged.summary

            if outcome is not None and outcome.ok:
                refreshed += 1
            results.append(
                {
                    "window_id": window_id,
                    "session_id": session_id,
                    "slice_count": row["slice_count"],
                    "decision": decision,
                    "reason": reason,
                    "ok": None if outcome is None else outcome.ok,
                    "warnings": list(outcome.warnings) if outcome is not None else [],
                }
            )

        return {
            "ok": True,
            "db": name,
            "candidates": len(candidates),
            "refreshed": refreshed,
            "results": results,
        }

    # ================================================================== 检索

    def search(
        self,
        query: str,
        db: str | None = None,
        top_k: int | None = None,
        tags: Sequence[str] | None = None,
        session: str = "default",
        collect_debug: bool = True,
    ) -> RetrievalResult:
        """检索。**只在当前记忆库内检索**——库与库是硬隔离边界。

        这里曾经有 `scope="all"` 的跨库检索（要靠一个开关打开）。去掉了：
        隔离是结构性保证，一个"能看到所有库"的检索口子会把它降级成默认值。
        需要另一个库的记忆就先在界面上切过去（切换是热的）。
        """
        name = db or self.ensure_default_db()

        # 新一轮检索 → 重置本轮回查预算
        self.usage.begin_round(name, session)

        components = self._components(name)
        result = components.pipeline.search(
            query, top_k=top_k, tags=tags, collect_debug=collect_debug
        )

        # 按 score 排序后截断：weight 参与的是 score，所以这一步不能省
        # （省掉会让"手工加权"静默失效）。
        ordered = sorted(result.hits, key=lambda hit: (-hit.score, hit.chunk_id))
        limit = top_k or self.settings.rerank_top_k

        payload = RetrievalResult(
            hits=ordered[:limit],
            # sessions 必须一起搬过来。这里新建了一个 result 而不是原地改，
            # 漏一个字段就会让它静默消失——会话命中已经因此丢过一次。
            sessions=result.sessions,
            # 图扩散的增补同样要显式带：expanded 不参与上面的排序截断
            # （它不是直接命中，混进去会被 score 重排毁掉图扩散的次序）。
            expanded=result.expanded,
            retrieval_mode=result.retrieval_mode,
            warnings=[f"[{name}] {w}" for w in result.warnings],
            suggestion=result.suggestion,
        )
        if collect_debug:
            payload.debug = {
                "per_db": {name: result.debug} if result.debug else {},
                "targets": [name],
                "query": query,
            }
        return payload

    def debug_search(
        self,
        query: str,
        db: str | None = None,
        top_k: int | None = None,
        tags: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """检索调试面板专用：一次拿到三路排名与全部参数。"""
        name = db or self.ensure_default_db()
        res = self.search(query, db=name, top_k=top_k, tags=tags)
        payload = {
            "query": query,
            "db": name,
            "retrieval_mode": res.retrieval_mode,
            "warnings": res.warnings,
            "params": res.debug.get("per_db", {}).get(name, {}).get("params", {}),
            "arms": res.debug.get("per_db", {}).get(name, {}).get("query_arms", {}),
            "vector": res.debug.get("per_db", {}).get(name, {}).get("vector", []),
            "lexical": res.debug.get("per_db", {}).get(name, {}).get("lexical", []),
            "fusion_pool": res.debug.get("per_db", {}).get(name, {}).get("fusion_pool", []),
            "reranked": res.debug.get("per_db", {}).get(name, {}).get("reranked", False),
            "final": [hit.to_debug_dict() for hit in res.hits],
            # 图扩散轨迹（§16）：种子、发射的候选、路径与触发原因。
            "expand": res.debug.get("per_db", {}).get(name, {}).get("expand", {}),
            "expanded": [hit.to_debug_dict() for hit in res.expanded],
            # 会话路的四栏 + 会话命中。与切片路并列展示，让面板能回答
            # "冷启动为什么命中了这个会话、为什么没命中那个"。
            "session_vector": res.debug.get("per_db", {})
            .get(name, {})
            .get("session_vector", []),
            "session_lexical": res.debug.get("per_db", {})
            .get(name, {})
            .get("session_lexical", []),
            "session_fusion_pool": res.debug.get("per_db", {})
            .get(name, {})
            .get("session_fusion_pool", []),
            "sessions": [hit.to_mcp_dict() for hit in res.sessions],
        }
        return payload

    # ================================================================== 回查

    def read_original(
        self,
        uid: str,
        db: str | None = None,
        mode: str = "quote",
        window: int | None = None,
        detail: str = "standard",
        session: str = "default",
    ) -> ReadResult:
        """回查原文。

        找不到 uid 时返回 `ok=False` 的 ReadResult 而不是抛异常——调用方
        （MCP / REST / CLI）都应该拿到统一形状的结果，工具层不该向模型抛错。
        """
        try:
            name, components = self._locate(uid, db)
        except DuramemError as exc:
            return ReadResult(ok=False, uid=uid, warnings=[str(exc)])
        result = components.reader.read(
            uid, mode=mode, window=window, detail=detail, session=session
        )
        if not result.ok and result.text == "":
            result.text = ""
        return result

    def read_neighbors(
        self, uid: str, db: str | None = None, before: int = 1, after: int = 1
    ) -> dict[str, Any]:
        name, components = self._locate(uid, db)
        payload = components.reader.read_neighbors(uid, before=before, after=after)
        payload["db"] = name
        return payload

    def _locate(
        self, uid: str, db: str | None, include_deleted: bool = True
    ) -> tuple[str, _Components]:
        """按 uid 找到所属库。

        `include_deleted` 默认 True：软删除的切片必须仍能被定位到，否则
        删了就永远恢复不了、回查也只会说"不存在"而不是"已删除"。
        需要排除已删除切片的场景由调用方显式传 False。
        """
        if db:
            components = self._components(db)
            if components.repo.get_chunk(uid, include_deleted=include_deleted) is None:
                raise DuramemError(f"库 {db} 中不存在切片 {uid}")
            return db, components

        for entry in self.registry.list():
            components = self._components(entry.display_name)
            if components.repo.get_chunk(uid, include_deleted=include_deleted) is not None:
                return entry.display_name, components
        raise DuramemError(
            f"未找到切片 {uid}。若本次检索是跨库的，请把结果里的 db 字段一并传入。"
        )

    # ================================================================== 写入

    def store(
        self,
        summary_text: str,
        original_text: str = "",
        db: str | None = None,
        title: str | None = None,
        keywords: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        weight: float = 1.0,
    ) -> dict[str, Any]:
        """手动写入一条切片。MCP 侧应限流，防止模型滥用。"""
        name = db or self.ensure_default_db()
        components = self._components(name)

        digest = content_hash(summary_text)
        existing = components.repo.find_alive_by_hash(digest)
        if existing is not None:
            return {
                "ok": True,
                "duplicate": True,
                "uid": existing["chunk_uid"],
                "db": name,
                "message": "内容与已有切片相同，未重复写入。",
            }

        draft = ChunkDraft(
            summary_text=summary_text,
            original_text=original_text,
            title=title or "",
            keywords=list(keywords or []),
            tags=list(tags or ["manual"]),
            weight=weight,
        )
        result = components.repo.insert_chunk(
            draft, source_db=components.db.db_uuid,
            summary_soft_limit=self.settings.summary_soft_limit_tokens,
        )

        warnings: list[str] = []
        text = components.pipeline._embedding_text(components.repo.get_chunk(result["uid"]) or {})
        try:
            vector = components.pipeline.embedder.embed_one(text)
            components.index.add(result["id"], vector)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"嵌入失败，切片已入库但暂缺向量：{exc}")

        payload = {"ok": True, "uid": result["uid"], "db": name, "duplicate": False}
        if warnings:
            payload["warnings"] = warnings
        return payload

    def forget(self, uid: str, db: str | None = None) -> dict[str, Any]:
        """软删除。前端可恢复。"""
        name, components = self._locate(uid, db, include_deleted=True)
        ok = components.repo.soft_delete(uid)
        return {
            "ok": ok,
            "uid": uid,
            "db": name,
            "message": "已软删除，可在前端恢复。" if ok else "切片不存在或已删除。",
        }

    def restore(self, uid: str, db: str | None = None) -> dict[str, Any]:
        name, components = self._locate(uid, db, include_deleted=True)
        ok = components.repo.restore(uid)
        return {"ok": ok, "uid": uid, "db": name}

    def purge_chunk(self, uid: str, db: str | None = None) -> dict[str, Any]:
        """**真删除**一条切片（连同向量），不可恢复。仅界面/CLI 调用，不给模型。

        - 原文（`messages`）不动：切片是派生数据，原文才是源；需要时重新总结能再生成。
        - 但**游标不会因此回退**，所以"删掉切片再重新总结"不会自动重建这一段——
          要重做就得先把该会话的 `summary_cursors` 回退（那是有意留给人做的事）。
        - 软删除的切片同样可以真删除（这正是"先软删、确认后再真删"的两段式）。
        """
        name, components = self._locate(uid, db, include_deleted=True)
        result = components.repo.hard_delete(uid)
        if not result.get("ok"):
            raise DuramemError(f"切片不存在：{uid}")

        warnings: list[str] = []
        vector_removed = False
        try:
            components.index.remove(int(result["chunk_id"]))
            vector_removed = True
        except Exception as exc:  # noqa: BLE001 - 行已经删了，孤儿向量不影响召回
            warnings.append(f"向量未能删除（只留下一条孤儿，不会被召回）：{exc}")

        payload: dict[str, Any] = {
            **result,
            "db": name,
            "vector_removed": vector_removed,
            "note": "切片已永久删除；原文（messages）未动，重新总结可再生成。",
        }
        if warnings:
            payload["warnings"] = warnings
        return payload

    def update_chunk(self, uid: str, db: str | None = None, **fields: Any) -> dict[str, Any]:
        """编辑切片。改到摘要/标题/关键词时自动重建分词，并按需重算向量。"""
        name, components = self._locate(uid, db, include_deleted=True)
        before = components.repo.get_chunk(uid, include_deleted=True)
        applied = components.repo.update_chunk(uid, **fields)

        need_reembed = bool({"summary_text", "title", "keywords", "tags"} & set(fields))
        warnings: list[str] = []
        if need_reembed and before is not None:
            after = components.repo.get_chunk(uid, include_deleted=True)
            if after is not None:
                try:
                    vector = components.pipeline.embedder.embed_one(
                        components.pipeline._embedding_text(after)
                    )
                    components.index.add(int(after["id"]), vector)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"向量未更新：{exc}")

        payload = {"ok": True, "uid": uid, "db": name, "applied": sorted(applied)}
        if warnings:
            payload["warnings"] = warnings
        return payload

    # ================================================================== 运维

    def stats(self, db: str | None = None) -> dict[str, Any]:
        name = db or self.ensure_default_db()
        components = self._components(name)
        stats = components.repo.stats()
        stats["db"] = name
        stats["index"] = {
            "backend": components.index.name,
            "dim": components.index.dim,
            "vectors": components.index.count(),
        }
        stats["rerank_enabled"] = components.pipeline.reranker.enabled
        stats["rerank_model"] = components.pipeline.reranker.model
        stats["embedding_provider"] = components.pipeline.embedder.model
        # 库内记录的来源 vs 当前生效的来源。两者不一致时凡是读过这里的界面
        # 都能提示"该重建了"——而带 stale 标记的库根本走不到这里（校验会拦）。
        stats["vector_source"] = stats.get("vector_source") or ""
        stats["configured_provider"] = self.providers.effective_embedding()["provider"]
        stats["cursors"] = components.repo.cursors()
        return stats

    def reindex(self, db: str | None = None, verify: bool = True) -> dict[str, Any]:
        name = db or self.ensure_default_db()
        components = self._components(name, verify=verify)
        return {"db": name, **components.pipeline.reindex_all()}

    def rebuild_links(self, db: str | None = None) -> dict[str, Any]:
        """整体重建被动链接（backfill）。

        链接是派生数据，与向量同理可重建；但它的输入是摘要/keywords/tags 本身，
        与嵌入模型无关——所以换模型不需要碰它，只有"链接机制上线前就存在的库"
        （或归档导入走了 --no-reindex 之类跳过派生物重建的路径）才需要这一步。
        新写入的切片在 insert/update 路径上已自动建链，这里主要是补存量。
        """
        name = db or self.ensure_default_db()
        components = self._components(name)
        result = components.repo.rebuild_links()
        return {"db": name, **result, "counts": components.repo.link_counts()}

    def rebuild_vectors(self, db: str | None = None) -> dict[str, Any]:
        """删掉并重建向量表，然后全量重新嵌入。

        这是错误信息里承诺的补救命令，用于两种情况：
        - 换了嵌入模型（维度可能变）
        - 改了 `VECTOR_CHUNK_SIZE`（chunk_size 是建表选项，只能重建）

        它**故意绕过**模型一致性校验——因为它的目的就是修正不一致。

        顺序很重要：先清空向量表 → 真去算一遍 → **算成功了才更新来源指纹**。
        反过来写（先记指纹再算）在接口不可用时会留下最坏的状态：
        元数据说"模型一致、向量可用"，而表里一条向量都没有，
        检索静默地少掉一整路召回。宁可留着"需重建"的标记让人再试一次。
        """
        name = db or self.ensure_default_db()
        entry = self.registry.get(name)
        provider = self.providers.effective_embedding()["provider"]

        # 绕过 _components（那里会做一致性校验），直接操作文件
        self._close_cached(name)

        database = Database(Path(entry.file_path), wal=self.settings.sqlite_wal)
        try:
            database.replace_vector_table(
                self.settings.embedding_dim, self.settings.vector_chunk_size
            )
        finally:
            database.close()

        # 这一句会真的调嵌入接口，是最可能失败的一步
        try:
            result = self.reindex(name, verify=False)
        except Exception:
            # 失败时务必把刚构造的那批"跳过校验"的组件清出缓存。
            # 留着它们的话，后续所有取用都命中缓存、再也不做一致性校验——
            # 库会以"元数据说不一致、但检索照样跑"的状态被使用，
            # 向量那一路静默返回空结果，没有任何提示。
            self._invalidate_cache()
            raise

        # 走到这里说明向量全部算出来了，现在才有资格宣告"来源就是当前提供方"
        database = Database(Path(entry.file_path), wal=self.settings.sqlite_wal)
        try:
            self._close_cached(name)
            database.update_embedding_model(self.settings.embedding_model, provider=provider)
        finally:
            database.close()

        return {
            "db": name,
            "embedding_model": self.settings.embedding_model,
            "embedding_dim": self.settings.embedding_dim,
            "vector_chunk_size": self.settings.vector_chunk_size,
            **result,
        }

    def embed_pending(self, db: str | None = None, limit: int = 256) -> dict[str, Any]:
        name = db or self.ensure_default_db()
        components = self._components(name)
        return {"db": name, **components.pipeline.embed_pending(limit=limit)}

    # ============================================ 导入/归档（门面 → importing.py）
    # 实现与设计取舍见 ImportExportDomain；这里只转发，保持调用面稳定。

    def importable_sources(self) -> dict[str, Any]:
        """本机上可以一键导入的位置。"""
        return self._importing.importable_sources()

    def list_importable_conversations(
        self,
        path: str | Path | None = None,
        source: str | None = None,
        db: str | None = None,
        include_subagents: bool = False,
        since_days: int | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """列出可选的对话，供用户挑选后再导入（带源解析缓存）。"""
        return self._importing.list_importable_conversations(
            path=path,
            source=source,
            db=db,
            include_subagents=include_subagents,
            since_days=since_days,
            refresh=refresh,
        )

    def import_conversations(
        self,
        path: str | Path | None = None,
        source: str | None = None,
        db: str | None = None,
        dry_run: bool = False,
        include_subagents: bool = False,
        sessions: Sequence[str] | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
        min_messages: int = 2,
        summarize: bool = False,
        max_messages: int | None = None,
    ) -> Any:
        """把已有对话导入记忆库（幂等，见 ImportExportDomain.import_conversations）。"""
        return self._importing.import_conversations(
            path=path,
            source=source,
            db=db,
            dry_run=dry_run,
            include_subagents=include_subagents,
            sessions=sessions,
            since_ms=since_ms,
            limit=limit,
            min_messages=min_messages,
            summarize=summarize,
            max_messages=max_messages,
        )

    def snapshot_database(
        self, db: str | None = None, target: str | Path | None = None
    ) -> dict[str, Any]:
        """checkpoint 后安全复制库文件（WAL 模式下直接拷会漏最近写入）。"""
        return self._importing.snapshot_database(db=db, target=target)

    def export_database(
        self, db: str | None = None, with_messages: bool = False
    ) -> dict[str, Any]:
        """导出为人类可读的 JSON 归档（向量不在其中，会话概览必须在）。"""
        return self._importing.export_database(db=db, with_messages=with_messages)

    def import_archive(
        self,
        path: str | Path,
        db: str | None = None,
        dry_run: bool = False,
        reindex: bool = True,
    ) -> dict[str, Any]:
        """把 `duramem export` 生成的归档还原成一个**新库**（文件路径入口）。"""
        return self._importing.import_archive(path, db=db, dry_run=dry_run, reindex=reindex)

    def import_archive_payload(
        self,
        payload: Any,
        db: str | None = None,
        dry_run: bool = False,
        reindex: bool = True,
    ) -> dict[str, Any]:
        """还原归档的实质路径（浏览器上传入口，与文件路径入口共用全部语义）。"""
        return self._importing.import_archive_payload(
            payload, db=db, dry_run=dry_run, reindex=reindex
        )

    # ================================================================== 设置

    def settings_view(self) -> dict[str, Any]:
        """设置页数据：可改项（含生效来源）+ 只读项 + 重启项。"""
        view = self.runtime.description()
        view["resolved"] = {
            "embedding_model": self.settings.embedding_model,
            "embedding_dim": self.settings.embedding_dim,
            # 用"实际生效"而不是配置里的开关：没填 Key 时它也是离线，
            # 但 embedding_fake 字段本身可能还是 False
            "offline_embedding": self.providers.effective_embedding()["offline"],
            "embedding_provider": self.providers.effective_embedding()["provider"],
            "rerank_model": self.settings.rerank_model,
            "summary_model": self.settings.summary_model,
            "gateway_enabled": self.settings.gateway_enabled,
            "data_dir": str(self.settings.data_dir),
        }
        return view

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """改运行时配置。返回 applied / rejected。"""
        result = self.runtime.update(patch)

        # 预算是有状态对象，需要跟着改（它不会自己重新读 settings）
        self.usage.soft_limit_tokens = max(0, self.settings.read_soft_limit_tokens)
        self.usage.idle_reset_seconds = max(0, self.settings.read_budget_idle_reset)

        # 重排开关影响的是组件构造期捕获的对象，就地替换而不是重建整个组件缓存
        if "rerank_enabled" in result["applied"]:
            self._rebuild_rerankers()

        return result

    def reset_settings(self, keys: list[str] | None = None) -> dict[str, Any]:
        """清除运行时覆盖，回落到 .env 默认值。"""
        fresh = Settings.load()
        result = self.runtime.reset(keys, settings_from_env=fresh)
        self.usage.soft_limit_tokens = max(0, self.settings.read_soft_limit_tokens)
        self.usage.idle_reset_seconds = max(0, self.settings.read_budget_idle_reset)
        self._rebuild_rerankers()
        return result

    def _rebuild_rerankers(self) -> None:
        with self._lock:
            for components in self._cache.values():
                components.pipeline.reranker = build_reranker(
                    enabled=self.settings.rerank_enabled,
                    api_key=self.settings.rerank_api_key,
                    base_url=self.settings.rerank_base_url,
                    model=self.settings.rerank_model,
                    segmenter=self.segmenter,
                )

    # ================================================================== 跨进程热重载

    @staticmethod
    def _file_stamp(path: Path) -> tuple[int, int] | None:
        """配置文件的指纹：mtime_ns + size。文件不存在返回 None。"""
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _maybe_hot_reload(self) -> None:
        """界面（另一个进程）改了运行时设置 / 模型配置时，本进程在下次
        工具调用里跟上。

        与 active_db 指针同一套办法：按文件指纹缓存，文件没变就只花一次
        stat()。挂在 `_components` 入口，MCP 的全部工具与 REST 的读写路径
        都会经过它。变更后的动作是完整重放启动时的复合顺序（见
        `_recompose_overrides`），但**不做只有写入方才该做的事**——provider
        侧的"标记向量作废"重复执行会把界面上刚重建完的向量再次作废，
        向量一致性交给 `_components` 里的 assert_compatible 把关。
        """
        runtime_stamp = self._file_stamp(self.runtime.path)
        provider_stamp = self._file_stamp(self.providers.path)
        if runtime_stamp == self._runtime_stamp and provider_stamp == self._provider_stamp:
            return
        with self._lock:
            if runtime_stamp != self._runtime_stamp:
                self._reload_runtime_settings()
                self._runtime_stamp = runtime_stamp
            if provider_stamp != self._provider_stamp:
                self._reload_provider_config()
                self._provider_stamp = provider_stamp

    def _reload_runtime_settings(self) -> None:
        """runtime_settings.json 被界面进程改写：重读并整体复合一次。"""
        self.runtime.load()
        self._recompose_overrides()

    def _reload_provider_config(self) -> None:
        """provider_config.json 被界面进程改写：重读并整体复合一次。"""
        self.providers.load()
        self._recompose_overrides()

    def _recompose_overrides(self) -> None:
        """把两份覆盖文件重新复合到 settings 上，顺序与启动时一致。

        先整体打回 .env 默认再依次套 runtime → provider 的覆盖值。不能只
        处理变化的那一份：两个白名单有交集（rerank_enabled），单独打回
        会把另一份文件里同字段的覆盖悄悄清掉。

        之后同步有状态对象（预算计数器捕获过旧值）并整批作废组件缓存——
        embedder / reranker / 摘要模型都是组件构造期捕获的。
        """
        for name, value in self._env_tunable_defaults.items():
            setattr(self.settings, name, value)
        for name, value in self._env_provider_defaults.items():
            setattr(self.settings, name, value)
        self.runtime.apply()
        self.providers.apply()
        self.usage.soft_limit_tokens = max(0, self.settings.read_soft_limit_tokens)
        self.usage.idle_reset_seconds = max(0, self.settings.read_budget_idle_reset)
        self._invalidate_cache()

    # ================================================================== 模型配置

    def provider_view(self) -> dict[str, Any]:
        """模型配置页数据：字段（密钥掩码）+ 预设 + 每张库的向量来源状态。"""
        view = self.providers.description()
        view["presets"] = {
            group: presets_for(group) for group in ("embedding", "rerank", "summary")
        }
        view["databases"] = self._vector_states()
        return view

    def _vector_states(self) -> list[dict[str, Any]]:
        """每张库：向量是谁生成的、要不要重建。

        只读 meta、不开 Repository——这里不该因为一张库的配置不一致就整体报错。
        """
        out: list[dict[str, Any]] = []
        pending = self.providers.effective_embedding()["provider"]
        for entry in self.registry.list():
            state: dict[str, Any] = {
                "name": entry.display_name,
                "file_path": entry.file_path,
                "vector_source": "",
                "chunks": 0,
                "vectors_stale": False,
                "needs_rebuild": False,
                "source_known": True,
                "error": "",
            }
            try:
                db = Database(Path(entry.file_path), wal=self.settings.sqlite_wal)
                try:
                    state["vector_source"] = db.vector_source
                    state["chunks"] = db.count_chunks()
                    state["vectors_stale"] = db.vectors_stale
                    source = db.vector_source
                    # 有向量却没记来源 = 本次改版之前建的库。它的真实来源
                    # 已经推不出来了，所以不猜：如实显示"未记录"。
                    state["source_known"] = bool(source) or state["chunks"] == 0
                    state["needs_rebuild"] = bool(
                        state["vectors_stale"] and source and source != pending
                    )
                finally:
                    db.close()
            except Exception as exc:  # noqa: BLE001 —— 单张库读不动不该拖垮整页
                state["error"] = _describe_open_failure(exc)
            out.append(state)
        return out

    def update_provider_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """改模型配置并热生效。

        换嵌入提供方是一步**会作废数据**的操作，所以分两步走：
        1. 记下改动前的有效提供方
        2. 改动真的影响了向量身份时，把所有已有向量的库标记为"需重建"

        第 2 步必须在改动**之前**拿到旧值——改完就再也推不出库里那批
        向量是谁生成的了。
        """
        before = self.providers.effective_embedding()
        result = self.providers.update(patch)

        touched = set(result["applied"]) & set(VECTOR_IDENTITY_FIELDS)
        if not touched:
            # 只是改了重排/摘要，或者只改了批量大小之类的非身份字段
            if "rerank_enabled" in result["applied"] or (
                set(result["applied"]) & {"rerank_model", "rerank_api_key", "rerank_base_url"}
            ):
                self._rebuild_rerankers()
            # 返回形状保持一致：调用方不该因为走了哪条分支而少拿到一个字段
            return {
                **result,
                "vectors_marked_stale": [],
                "identity_changed": False,
                "previous_provider": "",
                "effective_embedding": self.providers.effective_embedding(),
            }

        after = self.providers.effective_embedding()
        identity_changed = (before["provider"], before["dim"]) != (
            after["provider"],
            after["dim"],
        )

        marked: list[str] = []
        if identity_changed:
            marked = self._mark_all_vectors_stale(before["provider"], before["dim"])

        # 组件在构造期捕获了 embedder / index（含维度），必须整体重建
        self._invalidate_cache()
        return {
            **result,
            "vectors_marked_stale": marked,
            "identity_changed": identity_changed,
            "previous_provider": before["provider"] if identity_changed else "",
            "effective_embedding": after,
        }

    def reset_provider_config(self, keys: list[str] | None = None) -> dict[str, Any]:
        """清除模型配置覆盖，回落到 .env / 默认值。"""
        before = self.providers.effective_embedding()
        fresh = Settings.load()
        result = self.providers.reset(keys, settings_from_env=fresh)
        after = self.providers.effective_embedding()

        marked: list[str] = []
        identity_changed = (before["provider"], before["dim"]) != (
            after["provider"],
            after["dim"],
        )
        if identity_changed:
            marked = self._mark_all_vectors_stale(before["provider"], before["dim"])
        self._invalidate_cache()
        return {
            **result,
            "vectors_marked_stale": marked,
            "identity_changed": identity_changed,
            "effective_embedding": after,
        }

    def _mark_all_vectors_stale(self, provider: str, dim: int) -> list[str]:
        """把所有已有切片的库标记为"向量来源已变，需要重建"。

        空库跳过：它没有向量可作废，标记了只会让用户白跑一次重建。
        """
        source = provider or f"unknown-{dim}d"
        marked: list[str] = []
        for entry in self.registry.list():
            self._close_cached(entry.display_name)
            try:
                db = Database(Path(entry.file_path), wal=self.settings.sqlite_wal)
                try:
                    if db.count_chunks() > 0:
                        db.mark_vectors_stale(source)
                        marked.append(entry.display_name)
                finally:
                    db.close()
            except Exception:  # noqa: BLE001 —— 单张库动不了就跳过，别挡住配置生效
                continue
        return marked

    def _invalidate_cache(self) -> None:
        """关掉并丢弃所有已构造的组件。

        模型配置改了以后缓存里的 embedder / index 都是旧参数，必须整批重建。
        这里会关掉底层连接，所以调用方不能再持有旧的 _Components 引用。
        """
        with self._lock:
            for components in self._cache.values():
                components.db.close()
            self._cache.clear()

    def test_provider_config(
        self, patch: dict[str, Any], group: str = "embedding"
    ) -> dict[str, Any]:
        """拿**草稿**去试连通性，不改动任何已保存的配置。

        试草稿而不是试现值：用户填完点测试，测的必须是"如果保存会怎样"，
        否则测过之后再保存照样是坏的。
        """
        group = (group or "embedding").strip().lower()
        try:
            draft = self.providers.draft_settings(patch)
        except ValueError as exc:
            return {"ok": False, "group": group, "error": str(exc)}

        started = time.perf_counter()
        try:
            # 探测本体在 providers/connectivity.py：只依赖草稿配置与
            # providers/ 的实现，与服务状态无关。
            if group == "embedding":
                return probe_embedding(draft, self.segmenter, started)
            if group == "rerank":
                return probe_rerank(draft, started)
            if group == "summary":
                return probe_summary(draft, started)
        except Exception as exc:  # noqa: BLE001 —— 探测失败的形态很多，统一成一句话
            return {
                "ok": False,
                "group": group,
                "error": str(exc)[:600],
                "ms": int((time.perf_counter() - started) * 1000),
            }
        return {"ok": False, "group": group, "error": f"未知的分组：{group}"}


def _describe_open_failure(exc: Exception) -> str:
    """把"打不开这张库"翻译成人能读的一句话。

    `no such table: db_meta` 这种原始报错会被当成"程序坏了"，
    而实际原因通常是：数据目录里躺着一个空的或不是 Duramem 的 .db 文件，
    它被注册表收养了。
    """
    text = str(exc)
    if "db_meta" in text or "no such table" in text:
        return "不是 Duramem 库（文件里没有库结构），可以在库里把它移除"
    return text[:200]


__all__ = ["DuramemError", "Service"]
