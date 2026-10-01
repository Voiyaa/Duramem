"""服务层的导入/归档域：历史对话导入、库快照与归档导出/导入往返。

从 service.py 拆出（设计文档 A.15）：这是原 god object 里耦合最低的一块
自包含工作流——外部实现（`importers/`）与库操作（`store/`）都已各就各位，
这里只做编排。`Service` 保留同名门面方法转发到这里，api.py / mcp_server.py /
CLI 的调用面不变；改行为改这个文件。

领域不持有连接：全部库操作经 `Service` 的组件缓存（`_components`）走，
共享它的生命周期与热重载语义；唯一自有状态是对话列表缓存
（`_survey_cache`，带自己的锁——它不与 Service 的其他状态共变）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from duramem import __version__
from duramem.errors import DuramemError
from duramem.store.schema import update_meta

if TYPE_CHECKING:
    from duramem.service import Service


def _trim_conversations(conversations: Sequence[Any], max_messages: int) -> list[Any]:
    """每个会话只留最近 N 条消息。

    **保留原始的 seq** 而不是重新编号：seq 是消息在原会话里的位置。
    重新编号会让"先导入最近 50 条、之后再导入全部"产生两套互不重合的位置，
    同一批消息被当成两组写进去。保留原位置则天然对齐。
    """
    if max_messages <= 0:
        return list(conversations)

    trimmed: list[Any] = []
    for item in conversations:
        if len(item.messages) <= max_messages:
            trimmed.append(item)
            continue
        tail = list(item.messages[-max_messages:])
        # 不要让切片从助手的半句话开始
        while tail and tail[0].role != "user":
            tail = tail[1:]
        item.messages = tail
        trimmed.append(item)
    return trimmed


class ImportExportDomain:
    """历史导入 + 库快照 + 归档导出/导入。设计取舍见各方法的 docstring。"""

    EXPORT_FORMAT = "duramem-export"
    EXPORT_FORMAT_VERSION = 2

    def __init__(self, service: Service) -> None:
        self._service = service
        # 对话列表缓存：列出要走一遍源解析，ZCode 会话库要几秒，不能每次刷新都重算
        self._survey_cache: dict[str, dict[str, Any]] = {}
        self._survey_lock = threading.RLock()

    # ================================================================== 历史导入

    def importable_sources(self) -> dict[str, Any]:
        """本机上可以一键导入的位置。

        返回 dict 而不是 list：FastAPI 会按返回类型标注做响应校验，
        标注 dict 却返回 list 会让这个接口直接 500。
        """
        from duramem.importers import known_defaults, list_sources

        return {"sources": list_sources(), "defaults": known_defaults()}

    def list_importable_conversations(
        self,
        path: str | Path | None = None,
        source: str | None = None,
        db: str | None = None,
        include_subagents: bool = False,
        since_days: int | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """列出可选的对话，供用户挑选后再导入。

        带缓存（按"源 + 路径 + 文件修改时间"作键）：列出要走一遍源解析，
        ZCode 会话库要几秒，每次刷新界面都重算体验太差。
        """
        from duramem.importers import list_available
        from duramem.importers.transcripts import default_claude_dir, default_zcode_rollout_dir
        from duramem.importers.zcode_db import default_db_path

        target = Path(path).expanduser() if path else None
        if target is None:
            resolved = {
                "zcode": default_db_path(),
                "zcode-db": default_db_path(),
                "rollout": default_zcode_rollout_dir(),
                "zcode-rollout": default_zcode_rollout_dir(),
                "claude": default_claude_dir(),
                "claude-code": default_claude_dir(),
            }.get(source or "zcode")
            if resolved is None:
                raise DuramemError(f"源 {source} 需要显式提供路径")
            target = resolved

        # 缓存键包含修改时间，源变了自动失效
        try:
            stamp = target.stat().st_mtime if target.exists() else 0.0
        except OSError:
            stamp = 0.0
        cache_key = f"{source}|{target}|{stamp}|{include_subagents}|{since_days}|{db}"

        with self._survey_lock:
            cached = self._survey_cache.get(cache_key)
        if cached is not None and not refresh:
            return cached

        since_ms = None
        if since_days:
            from datetime import datetime, timedelta, timezone

            since_ms = int(
                (datetime.now(timezone.utc) - timedelta(days=int(since_days))).timestamp() * 1000
            )

        imported_ids: set[str] = set()
        try:
            name = db or self._service.ensure_default_db()
            rows = (
                self._service._components(name)
                .db.read_conn.execute("SELECT DISTINCT session_id FROM messages")
                .fetchall()
            )
            imported_ids = {str(row[0]) for row in rows}
        except Exception:  # noqa: BLE001 - 列不出已导入会话不该让整个列表失败
            imported_ids = set()

        try:
            importer_name, resolved_path, items = list_available(
                path=str(target),
                source=source,
                include_subagents=include_subagents,
                since_ms=since_ms,
                imported_ids=imported_ids,
            )
        except ValueError as exc:
            raise DuramemError(str(exc)) from exc

        payload = {
            "source": importer_name,
            "path": resolved_path,
            "total": len(items),
            "conversations": [item.to_payload() for item in items],
        }
        with self._survey_lock:
            self._survey_cache.clear()  # 只留最近一次，避免无界增长
            self._survey_cache[cache_key] = payload
        return payload

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
        """把已有对话导入记忆库。

        `sessions` 指定要导入哪些对话（用户挑中的那些）。不传则导入该源的全部。
        **导入是幂等的**：源会话 id 原样用作我们的 session_id，消息 seq 是它在
        原会话里的位置，重复内容的哈希一致——反复导入同一份历史不会产生重复记忆。
        """
        from duramem.importers import (
            build_report,
            detect_importer,
            get_importer,
            resolve_path,
        )
        from duramem.importers.base import ImportReport

        target, raw_source = resolve_path(str(path) if path else None, source)
        if not target.exists():
            return ImportReport(
                source=source or "auto",
                dry_run=dry_run,
                errors=[f"路径不存在：{target}"],
            )

        importer = get_importer(raw_source) if raw_source else detect_importer(target)
        if importer is None:
            return ImportReport(
                source=source or "auto",
                dry_run=dry_run,
                errors=[
                    f"无法识别该路径的格式：{target}。"
                    "可用 --source 显式指定（zcode / rollout / claude / generic）。"
                ],
            )

        conversations = list(
            importer.load(
                target,
                include_subagents=include_subagents,
                session_filter=list(sessions) if sessions else None,
                since_ms=since_ms,
                limit=limit,
            )
        )

        if max_messages:
            conversations = _trim_conversations(conversations, int(max_messages))

        report = build_report(importer.name, conversations, dry_run, min_messages)
        if dry_run:
            return report

        name = db or self._service.ensure_default_db()
        components = self._service._components(name)

        for item in conversations:
            if not item.messages:
                continue
            if len(item.messages) < min_messages:
                continue
            try:
                accepted = components.repo.upsert_messages(item.messages)
                report.conversations_imported += 1
                report.messages_imported += len(accepted)

                if summarize:
                    outcome = components.summarizer.summarize(
                        item.window_id,
                        item.session_id,
                        source_db=components.db.db_uuid,
                    )
                    if outcome.ok:
                        report.summarized += 1
                        report.chunks_added += outcome.added
            except Exception as exc:  # noqa: BLE001 - 单个会话失败不该中断整批导入
                report.errors.append(f"{item.origin_id}：{exc}")

        return report

    # ================================================================== 库快照

    def snapshot_database(
        self, db: str | None = None, target: str | Path | None = None
    ) -> dict[str, Any]:
        """checkpoint 后安全复制库文件。

        "一个文件 = 一份记忆"要成立，就不能让用户去手工拷一个 WAL 模式的 .db
        ——那会漏掉最近写入的内容。
        """
        name = db or self._service.ensure_default_db()
        components = self._service._components(name)
        destination = Path(
            target or (self._service.settings.data_dir / f"{name}.snapshot.db")
        )
        components.db.snapshot_to(destination)
        return {"ok": True, "db": name, "path": str(destination),
                "size_bytes": destination.stat().st_size}

    # ================================================== 归档导出 / 导入

    def export_database(
        self, db: str | None = None, with_messages: bool = False
    ) -> dict[str, Any]:
        """导出为人类可读的 JSON 归档（`import_archive` 的对应物，往返闭合）。

        向量**不在**归档里：它们可由当前提供方全量重建，放进去只会把文件撑大
        一个数量级、还把归档绑死在某个嵌入模型上。会话概览（L1）**必须在**：
        那是花模型调用生成的，丢了就要重新生成——v2 归档缺 cursors/layers
        就是这个教训，format_version 2 补上了。
        """
        name = db or self._service.ensure_default_db()
        components = self._service._components(name)
        chunks = components.repo.list_chunks(
            include_deleted=True, include_superseded=True, limit=1_000_000
        )
        payload: dict[str, Any] = {
            "format": self.EXPORT_FORMAT,
            "format_version": self.EXPORT_FORMAT_VERSION,
            "version": __version__,
            "db": name,
            "meta": components.db.meta,
            "sessions": components.repo.sessions(),
            "cursors": components.repo.cursors(),
            "layers": components.session_layers.export_rows(),
            "chunks": chunks,
            "runs": components.repo.summary_runs(limit=200),
        }
        if with_messages:
            payload["messages"] = components.repo.all_messages()
        return payload

    def import_archive(
        self,
        path: str | Path,
        db: str | None = None,
        dry_run: bool = False,
        reindex: bool = True,
    ) -> dict[str, Any]:
        """把 `duramem export` 生成的归档还原成一个**新库**（文件路径入口）。"""
        archive = Path(path)
        if not archive.exists():
            raise DuramemError(f"归档文件不存在：{archive}")
        try:
            payload = json.loads(archive.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DuramemError(f"归档读不出来（{exc}）：{archive}") from exc
        return self.import_archive_payload(payload, db=db, dry_run=dry_run, reindex=reindex)

    def import_archive_payload(
        self,
        payload: Any,
        db: str | None = None,
        dry_run: bool = False,
        reindex: bool = True,
    ) -> dict[str, Any]:
        """还原归档的实质路径。与 `import_archive` 分开：浏览器上传拿不到
        服务器侧文件路径，只能把归档内容原样发上来——两条入口共用全部语义。"""
        service = self._service
        if not isinstance(payload, dict) or payload.get("format") != self.EXPORT_FORMAT:
            fmt = payload.get("format") if isinstance(payload, dict) else type(payload).__name__
            raise DuramemError(
                f"不是 Duramem 导出归档（format={fmt!r}）。"
                "归档由 `duramem export` 或界面上的「导出归档」生成。"
            )
        try:
            version = int(payload.get("format_version") or 1)
        except (TypeError, ValueError):
            version = 1
        warnings: list[str] = []
        if version > self.EXPORT_FORMAT_VERSION:
            raise DuramemError(
                f"归档格式版本更新（{version} > {self.EXPORT_FORMAT_VERSION}），"
                "请先升级 Duramem 再导入。"
            )
        if version < self.EXPORT_FORMAT_VERSION:
            warnings.append(
                f"归档是旧格式（format_version={version}）：不含游标与会话概览，"
                "这两样导入后为空，需要重新生成概览。"
            )

        chunks = [row for row in (payload.get("chunks") or []) if isinstance(row, dict)]
        messages = [row for row in (payload.get("messages") or []) if isinstance(row, dict)]
        layers = [row for row in (payload.get("layers") or []) if isinstance(row, dict)]
        cursors = [row for row in (payload.get("cursors") or []) if isinstance(row, dict)]
        runs = [row for row in (payload.get("runs") or []) if isinstance(row, dict)]

        target = (db or "").strip() or str(payload.get("db") or "").strip()
        if not target:
            raise DuramemError(
                "无法确定目标库名：归档里没有 db 字段，调用时也未指定（--db / 请求体 db）。"
            )
        if service.registry.exists(target):
            raise DuramemError(
                f"库已存在：{target}。归档只还原到新库，不与已有库合并；"
                "请换一个名字，或先在界面上处理掉同名库。"
            )

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "db": target,
                "format_version": version,
                "chunks": len(chunks),
                "messages": len(messages),
                "layers": len(layers),
                "cursors": len(cursors),
                "runs": len(runs),
                "warnings": warnings,
            }

        # 新库 = 新 db_uuid：还原出的是一份**副本**。沿用源 uuid 的话，两个文件
        # 带同一个身份，discover() 会把其中一份当成另一份的迁移结果而改指路径。
        service.registry.create(target, service.settings)
        components = service._components(target)

        counts: dict[str, int] = {}
        if messages:
            counts["messages"] = components.repo.restore_messages(messages)
        elif any(c.get("msg_id_start") is not None for c in chunks):
            warnings.append(
                "归档未含原始消息（导出时未带 --with-messages / with_messages）："
                "切片的 msg 区间指向的消息不存在，window 模式回查不可用，quote 不受影响。"
            )

        chunk_stats = components.repo.restore_chunks(chunks)
        counts["chunks"] = chunk_stats["restored"]
        if chunk_stats["skipped_empty"]:
            warnings.append(f"{chunk_stats['skipped_empty']} 条切片缺 summary_text，已跳过。")
        if chunk_stats["duplicates"]:
            warnings.append(
                f"{chunk_stats['duplicates']} 条切片 uid 缺失或在归档内重复，已跳过。"
            )

        # 区间指针的完整性要如实报告：指向归档之外的消息 id 意味着源库在导出后
        # 清理过消息，那些切片的 window 回查会缺内容——不是错误，但不能装没有
        if messages:
            message_ids = {
                int(row["id"]) for row in messages if row.get("id") is not None
            }
            dangling = sum(
                1
                for c in chunks
                if c.get("msg_id_start") is not None
                and str(c.get("msg_id_start")).lstrip("-").isdigit()
                and int(c["msg_id_start"]) not in message_ids
            )
            if dangling:
                warnings.append(
                    f"{dangling} 条切片的 msg 区间指向归档外的消息 id，"
                    "这些区间的 window 回查会缺内容。"
                )

        counts["layers"] = components.session_layers.restore(layers)
        counts["cursors"] = components.repo.restore_cursors(cursors)
        counts["runs"] = components.repo.restore_runs(runs)

        # 库的"出生时间"属于记忆本身，不是本次导入的时间
        original_created = str((payload.get("meta") or {}).get("created_at") or "").strip()
        if original_created:
            with components.db.write() as conn:
                update_meta(conn, created_at=original_created)

        if reindex:
            vector_result = service.rebuild_vectors(target)
            counts["vectors"] = int(vector_result.get("embedded") or 0)
            counts["session_vectors"] = int(vector_result.get("sessions_embedded") or 0)
        else:
            # 不留"向量表是空的但校验通过"的假正常状态：空向量路会静默退化成
            # 纯词法检索，界面上看不出任何异常。标上来源指纹让检索大声拒绝，
            # 直到执行 rebuild-vectors。
            components.db.mark_vectors_stale("archive-import")
            # 上面那份组件是在标记**之前**构造并通过校验的，缓存里的它不会重新
            # 校验——不作废的话，下一次 search 命中缓存、照样放行，等于白标。
            service._close_cached(target)
            warnings.append(
                "已跳过向量重建：执行 `duramem rebuild-vectors --db "
                f"{target}` 之前，该库检索只有词法路。"
            )

        return {
            "ok": True,
            "db": target,
            "format_version": version,
            **counts,
            "warnings": warnings,
        }
