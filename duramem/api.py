"""FastAPI：给前端管理界面用的 REST 接口。

与 MCP 共用 Service 层，保证两条入口能力对等。
所有接口都是同步 `def`——FastAPI 会自动放进线程池，避免阻塞事件循环。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from duramem import __version__
from duramem.config import Settings, get_settings
from duramem.models import Message
from duramem.service import DuramemError, Service

# ====================================================================== 请求体


class CreateDatabaseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class RenameDatabaseRequest(BaseModel):
    new_name: str = Field(min_length=1, max_length=64)
    rename_file: bool = False


class ColdStartRequest(BaseModel):
    """每库的冷启动注入配置。note/enabled 传 null 表示不改动该项。"""

    note: str | None = Field(default=None, max_length=8000)
    enabled: bool | None = None


class ActiveDatabaseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class MessageIn(BaseModel):
    window_id: str
    session_id: str
    seq: int
    role: str
    content: str
    speaker: str | None = None
    ts: str | None = None
    source_msg_id: str | None = None


class IngestRequest(BaseModel):
    messages: list[MessageIn]
    db: str | None = None
    auto_summarize: bool = True


class SummarizeRequest(BaseModel):
    window_id: str
    session_id: str
    db: str | None = None
    messages: list[MessageIn] | None = None


class SessionLayerRefreshRequest(BaseModel):
    """批量刷新会话概览。"""

    session_id: str | None = None
    window_id: str | None = None
    force: bool = False
    limit: int = 0


class SearchRequest(BaseModel):
    query: str
    db: str | None = None
    top_k: int | None = None
    tags: list[str] | None = None
    collect_debug: bool = True


class ChunkPatchRequest(BaseModel):
    title: str | None = None
    summary_text: str | None = None
    original_text: str | None = None
    keywords: list[str] | None = None
    tags: list[str] | None = None
    weight: float | None = None
    title_suggested: str | None = None


class ImportSurveyRequest(BaseModel):
    source: str | None = None
    path: str | None = None
    db: str | None = None
    include_subagents: bool = False
    since_days: int | None = None
    refresh: bool = False


class ImportRequest(BaseModel):
    source: str | None = None
    path: str | None = None
    db: str | None = None
    sessions: list[str] = Field(default_factory=list)
    summarize: bool = True
    dry_run: bool = False
    max_messages: int | None = None
    min_messages: int = 2
    include_subagents: bool = False


class ImportArchiveRequest(BaseModel):
    """导入导出归档（`duramem export` 的产物），还原成一个新库。"""

    path: str = Field(min_length=1)
    db: str | None = None
    dry_run: bool = False
    reindex: bool = True


def _install_error_handlers(app: FastAPI) -> None:
    """把"模型接口挂了"翻译成带原因的错误响应。

    不装这些处理器时，嵌入/重排/摘要接口的任何失败都会变成 HTTP 500
    "Internal Server Error"——前端只能原样显示这六个字。用户看到的是
    "重建失败了"，但不知道是密钥错了、模型名错了还是网络不通，
    而这三个原因的处置方式完全不同。

    502 而不是 500：语义上是"上游模型服务没成功"，不是本服务的代码出错。
    """

    from fastapi.responses import JSONResponse

    from duramem.providers.embedding import EmbeddingError
    from duramem.providers.rerank import RerankError
    from duramem.store.schema import EmbeddingMismatchError
    from duramem.summarizer import SummaryError

    def _provider_failure(request: Any, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "detail": f"{type(exc).__name__}：{exc}",
                "kind": "provider_failure",
                "provider": type(exc).__name__,
            },
        )

    def _mismatch(request: Any, exc: Exception) -> JSONResponse:
        # 一致性校验失败是"状态不对"，不是上游故障：409 + 原始说明
        return JSONResponse(
            status_code=409, content={"detail": str(exc), "kind": "embedding_mismatch"}
        )

    def _bad_state(request: Any, exc: Exception) -> JSONResponse:
        # 业务状态不对（当前记忆库指针指向一个已删的库、库名不存在……）：
        # 400 + 原话。不装这个处理器会变成 500 "Internal Server Error"，
        # 而用户需要看到的是"当前记忆库 X 已不存在，请重新选定"。
        return JSONResponse(
            status_code=400, content={"detail": str(exc), "kind": "bad_state"}
        )

    for exc_type in (EmbeddingError, RerankError, SummaryError):
        app.add_exception_handler(exc_type, _provider_failure)
    app.add_exception_handler(EmbeddingMismatchError, _mismatch)
    app.add_exception_handler(DuramemError, _bad_state)


def create_app(service: Service | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    svc = service or Service(settings)

    app = FastAPI(
        title="Duramem",
        description="挂载式 LLM 记忆服务 —— 切片即记忆，同时作为原文索引",
        version=__version__,
    )
    app.state.service = svc

    _install_error_handlers(app)

    # ================================================================ 基础

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        effective = svc.providers.effective_embedding()
        return {
            "ok": True,
            "version": __version__,
            "databases": [e.display_name for e in svc.registry.list()],
            "embedding_model": svc.settings.embedding_model,
            # 这个才是事实：没填 Key 时实际用的是离线哈希，而不是上面那个模型名
            "embedding_provider": effective["provider"],
            "offline_embedding": effective["offline"],
            "rerank_enabled": svc.settings.rerank_enabled,
        }

    @app.get("/api/settings")
    def read_settings() -> dict[str, Any]:
        return svc.settings_view()

    @app.patch("/api/settings")
    def patch_settings(payload: dict[str, Any]) -> dict[str, Any]:
        """改运行时配置（白名单字段）。密钥类字段不可从界面修改。"""
        result = svc.update_settings(payload)
        result["ok"] = not result["rejected"]
        return result

    @app.post("/api/settings/reset")
    def reset_settings(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        keys = (payload or {}).get("keys")
        return svc.reset_settings(keys)

    # ================================================================ 模型配置

    @app.get("/api/provider-config")
    def read_provider_config() -> dict[str, Any]:
        return svc.provider_view()

    @app.patch("/api/provider-config")
    def patch_provider_config(payload: dict[str, Any]) -> dict[str, Any]:
        """改模型配置并热生效。

        密钥字段只在写入时出现，读回永远是掩码。换嵌入提供方会把已有向量的库
        标记为"需重建"，返回里的 `vectors_marked_stale` 就是这批库名。
        """
        result = svc.update_provider_config(payload)
        result["ok"] = not result["rejected"]
        return result

    @app.post("/api/provider-config/reset")
    def reset_provider_config(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        keys = (payload or {}).get("keys")
        return svc.reset_provider_config(keys)

    @app.post("/api/provider-config/test")
    def test_provider_config(payload: dict[str, Any]) -> dict[str, Any]:
        """拿草稿试连通性（不保存）。字段用 `values`，分组用 `group`。"""
        return svc.test_provider_config(
            payload.get("values") or {}, payload.get("group") or "embedding"
        )

    # ================================================================ 库管理

    @app.get("/api/databases")
    def list_databases() -> dict[str, Any]:
        return {"databases": svc.list_databases()}

    @app.get("/api/active-db")
    def read_active_db() -> dict[str, Any]:
        """当前记忆库（前端选定、MCP 跟随的那个库）。

        `source` 说明这个值是哪来的：`active_db.json`（界面选定）/ `none`（还没选过）。
        前端据此显示"当前记忆库"徽章，并在指针失效时给出重新选定的入口。
        """
        info = svc.active_view()
        info["fallback"] = None
        if not info["db"] or not info["exists"]:
            try:
                info["fallback"] = svc.ensure_default_db()
            except DuramemError as exc:
                info["error"] = str(exc)
        return info

    @app.post("/api/active-db")
    def set_active_db(payload: ActiveDatabaseRequest) -> dict[str, Any]:
        """选定当前记忆库。写进 data/active_db.json，MCP 侧下一轮检索就会切过去。"""
        try:
            return {"ok": True, **svc.set_active_db(payload.name, by="ui")}
        except DuramemError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/databases")
    def create_database(payload: CreateDatabaseRequest) -> dict[str, Any]:
        try:
            return svc.create_database(payload.name)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.patch("/api/databases/{name}")
    def rename_database(name: str, payload: RenameDatabaseRequest) -> dict[str, Any]:
        try:
            return svc.rename_database(name, payload.new_name, rename_file=payload.rename_file)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/databases/{name}/cold-start")
    def get_cold_start(name: str) -> dict[str, Any]:
        try:
            return svc.cold_start_view(name)
        except (DuramemError, KeyError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/mcp/last-call")
    def mcp_last_call() -> dict[str, Any]:
        """最近一次 MCP 工具调用的输入与输出（调试页展示；MCP 子进程落盘）。"""
        return svc.last_mcp_call()

    @app.patch("/api/databases/{name}/cold-start")
    def patch_cold_start(name: str, payload: ColdStartRequest) -> dict[str, Any]:
        """写某库的冷启动注入配置（note / enabled，null = 不改该项）。"""
        try:
            return svc.set_cold_start(name, note=payload.note, enabled=payload.enabled)
        except (DuramemError, KeyError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/databases/{name}")
    def delete_database(name: str, purge_file: bool = False) -> dict[str, Any]:
        try:
            return svc.delete_database(name, purge_file=purge_file)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/databases/{name}/stats")
    def database_stats(name: str) -> dict[str, Any]:
        try:
            full = svc.stats(name)
            # brief 是前端状态卡与模型 dm_stats 共用的精简投影（同一份事实）；
            # 全量字段仍保留在响应里，供管理页其他角落使用
            full["brief"] = svc.stats_brief(full, svc.active_view())
            return full
        except (DuramemError, KeyError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ================================================================ 采集

    @app.post("/api/ingest")
    def ingest(payload: IngestRequest) -> dict[str, Any]:
        messages = [
            Message(
                window_id=m.window_id,
                session_id=m.session_id,
                seq=m.seq,
                role=m.role,
                content=m.content,
                speaker=m.speaker,
                ts=m.ts,
                source_msg_id=m.source_msg_id,
            )
            for m in payload.messages
        ]
        try:
            return svc.ingest(messages, db=payload.db, auto_summarize=payload.auto_summarize)
        except DuramemError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/summarize")
    def summarize(payload: SummarizeRequest) -> dict[str, Any]:
        messages = None
        if payload.messages:
            messages = [
                Message(
                    window_id=m.window_id,
                    session_id=m.session_id,
                    seq=m.seq,
                    role=m.role,
                    content=m.content,
                    speaker=m.speaker,
                    ts=m.ts,
                    source_msg_id=m.source_msg_id,
                )
                for m in payload.messages
            ]
        try:
            outcome = svc.summarize(
                payload.window_id, payload.session_id, db=payload.db, messages=messages
            )
            return outcome.to_payload()
        except DuramemError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/summary-runs")
    def summary_runs(db: str | None = None, limit: int = 50) -> dict[str, Any]:
        components = svc._components(db or svc.ensure_default_db())
        return {"runs": components.repo.summary_runs(limit=limit)}

    # ================================================================ 会话与消息

    @app.get("/api/sessions")
    def sessions(db: str | None = None) -> dict[str, Any]:
        components = svc._components(db or svc.ensure_default_db())
        return {"sessions": components.repo.sessions()}

    @app.get("/api/messages")
    def messages(
        window_id: str,
        session_id: str,
        db: str | None = None,
        limit: int = Query(default=200, le=2000),
    ) -> dict[str, Any]:
        components = svc._components(db or svc.ensure_default_db())
        rows = components.db.read_conn.execute(
            "SELECT * FROM messages WHERE window_id=? AND session_id=? ORDER BY id LIMIT ?",
            (window_id, session_id, limit),
        ).fetchall()
        return {"messages": [dict(r) for r in rows]}

    # ================================================================ 切片

    @app.get("/api/chunks")
    def list_chunks(
        db: str | None = None,
        query: str | None = None,
        tags: list[str] | None = Query(default=None),  # noqa: B008 —— FastAPI 依赖注入惯用法
        include_deleted: bool = False,
        include_superseded: bool = False,
        # 上限放宽到 2000：时间线页要一次拿到全部切片做聚合，500 会在真实使用中
        # 直接触发 422（而 422 的错误体是数组，前端一旦格式化不当就只剩
        # "[object Object]"，把问题藏起来）。本地单用户的量级下这个上限是安全的。
        limit: int = Query(default=50, le=2000),
        offset: int = 0,
    ) -> dict[str, Any]:
        components = svc._components(db or svc.ensure_default_db())
        rows = components.repo.list_chunks(
            query=query,
            tags=tags,
            include_deleted=include_deleted,
            include_superseded=include_superseded,
            limit=limit,
            offset=offset,
        )
        # 计数用与列表完全相同的过滤条件，否则"共 N 条…显示前 M"会自相矛盾
        total = components.repo.count_chunks_matching(
            query=query,
            tags=tags,
            include_deleted=include_deleted,
            include_superseded=include_superseded,
        )
        return {"chunks": rows, "total": total}

    @app.get("/api/chunks/{uid}")
    def get_chunk(uid: str, db: str | None = None, with_original: bool = True) -> dict[str, Any]:
        name = db or svc.ensure_default_db()
        components = svc._components(name)
        chunk = components.repo.get_chunk(uid, include_deleted=True)
        if chunk is None:
            raise HTTPException(status_code=404, detail=f"切片不存在：{uid}")
        if not with_original or not svc.settings.original_access_enabled:
            chunk = {**chunk, "original_text": ""}
        chunk["db"] = name
        chunk["neighbours"] = [
            {"uid": n["chunk_uid"], "title": n["title"], "summary": n["summary_text"]}
            for n in components.repo.neighbours(chunk, before=1, after=1)
            if n["chunk_uid"] != uid
        ]
        return chunk

    @app.patch("/api/chunks/{uid}")
    def patch_chunk(uid: str, payload: ChunkPatchRequest, db: str | None = None) -> dict[str, Any]:
        fields = payload.model_dump(exclude_none=True)
        if not fields:
            raise HTTPException(status_code=400, detail="没有提供任何要修改的字段")
        try:
            return svc.update_chunk(uid, db=db, **fields)
        except DuramemError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/chunks/{uid}")
    def delete_chunk(uid: str, db: str | None = None, purge: bool = False) -> dict[str, Any]:
        """默认**软删除**（可恢复）。`?purge=true` 才是真删除，不可恢复。

        真删除只开给界面与 CLI：模型那边永远只有软删除（`dm_forget`），
        否则它一次误判就把记忆彻底抹掉了。
        """
        try:
            if purge:
                return svc.purge_chunk(uid, db=db)
            return svc.forget(uid, db=db)
        except DuramemError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/chunks/{uid}/restore")
    def restore_chunk(uid: str, db: str | None = None) -> dict[str, Any]:
        try:
            return svc.restore(uid, db=db)
        except DuramemError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ================================================================ 检索

    @app.post("/api/search")
    def search(payload: SearchRequest) -> dict[str, Any]:
        try:
            result = svc.search(
                payload.query,
                db=payload.db,
                top_k=payload.top_k,
                tags=payload.tags,
                collect_debug=payload.collect_debug,
            )
            body = result.to_mcp_payload()
            if payload.collect_debug:
                body["debug"] = result.debug
            return body
        except DuramemError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/debug/search")
    def debug_search(payload: SearchRequest) -> dict[str, Any]:
        """检索调试面板：向量 / 词法 / RRF 三路排名并列。

        这是自己实现融合（而非用一体化引擎）的直接收益——一体化引擎把融合
        做在内部，中间排名就不透明了。
        """
        try:
            return svc.debug_search(
                payload.query, db=payload.db, top_k=payload.top_k, tags=payload.tags
            )
        except DuramemError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ============================================================== 会话层

    @app.get("/api/session-layers")
    def list_session_layers(db: str | None = None) -> dict[str, Any]:
        """列出会话层（容器层）。切片浏览器之外的一个视角：按会话看记忆。"""
        return {"sessions": svc.list_session_layers(db=db)}

    @app.get("/api/session-layers/{session_id}")
    def get_session_layer(
        session_id: str, db: str | None = None, window_id: str | None = None
    ) -> dict[str, Any]:
        row = svc.get_session_layer(session_id, db=db, window_id=window_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"没有该会话的概览：{session_id}"
            )
        return row

    @app.post("/api/session-layers/refresh")
    def refresh_session_layers(
        payload: SessionLayerRefreshRequest, db: str | None = None
    ) -> dict[str, Any]:
        """按 freshness 策略刷新。给 session_id 时只处理那一个。

        刻意不做成自动触发：hook 是同步的（每轮已多约 0.9 秒），在采集路径上
        塞模型调用会拖慢每一轮对话。所以它是个显式动作。
        """
        if payload.session_id:
            window = payload.window_id
            layer = svc.get_session_layer(payload.session_id, db=db, window_id=window)
            if layer is None and window is None:
                raise HTTPException(
                    status_code=404, detail=f"没有该会话的概览：{payload.session_id}"
                )
            outcome = svc.summarize_session(
                layer["window_id"] if layer else str(window),
                payload.session_id,
                db=db,
            )
            return {"ok": outcome.ok, "refreshed": 1 if outcome.ok else 0,
                    "results": [{"session_id": payload.session_id,
                                 "ok": outcome.ok,
                                 "warnings": list(outcome.warnings)}]}
        return svc.refresh_sessions(db=db, limit=payload.limit, force=payload.force)

    # ================================================================ 运维

    @app.post("/api/reindex")
    def reindex(db: str | None = None) -> dict[str, Any]:
        return svc.reindex(db)

    @app.post("/api/links/rebuild")
    def links_rebuild(db: str | None = None) -> dict[str, Any]:
        """整体重建被动链接（backfill）。新切片自动建链，这条补存量。"""
        return svc.rebuild_links(db)

    @app.post("/api/rebuild-vectors")
    def rebuild_vectors(db: str | None = None) -> dict[str, Any]:
        """删除并重建向量表后全量重新嵌入。

        换嵌入模型或改 VECTOR_CHUNK_SIZE 之后用——两者都只能重建，不能原地改。
        """
        try:
            return svc.rebuild_vectors(db)
        except (KeyError, DuramemError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/embed-pending")
    def embed_pending(db: str | None = None, limit: int = 256) -> dict[str, Any]:
        return svc.embed_pending(db, limit=limit)

    @app.post("/api/snapshot")
    def snapshot(db: str | None = None, target: str | None = None) -> dict[str, Any]:
        """checkpoint 后安全复制库文件（"一个文件 = 一份记忆"的安全导出）。"""
        return svc.snapshot_database(db, target)

    # ============================================================ 归档导出/导入

    @app.get("/api/export")
    def export_archive(db: str | None = None, with_messages: bool = False):
        """导出可读 JSON 归档（Content-Disposition 触发浏览器下载）。

        向量不在归档里（导入端按当前提供方重建）；会话概览与游标在——
        那是花模型调用生成的，往返丢了就要重新生成。
        """
        payload = svc.export_database(db=db, with_messages=with_messages)
        filename = quote(f"{payload['db']}-export.json")
        return JSONResponse(
            payload,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.post("/api/import-archive")
    def import_archive(payload: ImportArchiveRequest) -> dict[str, Any]:
        """把归档还原成一个新库。只进新库、不合并（uid/消息 id/游标是一套咬合的寻址体系）。"""
        try:
            return svc.import_archive(
                path=payload.path,
                db=payload.db,
                dry_run=payload.dry_run,
                reindex=payload.reindex,
            )
        except DuramemError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/import-archive/upload")
    async def import_archive_upload(
        request: Request,
        db: str | None = None,
        dry_run: bool = False,
        reindex: bool = True,
    ) -> dict[str, Any]:
        """浏览器上传归档还原（前端拿不到服务器侧路径，只能发归档内容本身）。

        请求体就是归档 JSON 原文；库名等参数走 query。与 path 版共用全部语义。
        """
        import json as _json

        try:
            payload = _json.loads(await request.body())
        except (UnicodeDecodeError, _json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"归档不是合法 JSON：{exc}") from exc
        try:
            return svc.import_archive_payload(
                payload, db=db, dry_run=dry_run, reindex=reindex
            )
        except DuramemError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ================================================================ 历史导入

    @app.get("/api/import/sources")
    def import_sources() -> dict[str, Any]:
        """可用的导入源，以及本机上存在的位置。"""
        return svc.importable_sources()

    @app.post("/api/import/list")
    def import_list(payload: ImportSurveyRequest) -> dict[str, Any]:
        """列出可选的对话（不写库）。

        前端用它渲染选择列表：用户先看到有什么、再勾选要导入哪些。
        默认排除子代理会话——那是主代理派出去的任务，不是用户自己的对话。
        """
        try:
            return svc.list_importable_conversations(
                path=payload.path,
                source=payload.source,
                db=payload.db,
                include_subagents=payload.include_subagents,
                since_days=payload.since_days,
                refresh=payload.refresh,
            )
        except DuramemError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/import")
    def do_import(payload: ImportRequest) -> dict[str, Any]:
        """导入用户选中的对话。不传 sessions 会拒绝——避免一次灌进整个历史。"""
        if not payload.sessions and not payload.dry_run:
            raise HTTPException(
                status_code=400,
                detail="需要指定要导入的对话（sessions 不能为空）。先调 /api/import/list 拿列表。",
            )
        try:
            report = svc.import_conversations(
                path=payload.path,
                source=payload.source,
                db=payload.db,
                dry_run=payload.dry_run,
                include_subagents=payload.include_subagents,
                sessions=payload.sessions or None,
                min_messages=payload.min_messages,
                summarize=payload.summarize,
                max_messages=payload.max_messages,
            )
            return report.to_payload()
        except DuramemError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ================================================================ 记忆网关

    if settings.gateway_enabled and settings.gateway_upstream_base_url:
        from duramem.collectors.gateway import create_gateway_router

        app.include_router(
            create_gateway_router(
                service=svc,
                upstream_base_url=settings.gateway_upstream_base_url,
                upstream_api_key=settings.gateway_upstream_api_key,
                default_db=settings.default_db,
            )
        )

    @app.on_event("shutdown")
    def _shutdown() -> None:
        svc.close()

    # 前端构建产物：存在就一并托管，`duramem serve` 一条命令即可用。
    # 必须在所有 /api 路由之后挂载，否则会把 API 路由遮住。
    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if dist.exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")

    return app


app = None  # 由 `duramem serve` 通过 create_app() 构造，避免导入时即建连


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        create_app(settings=settings),
        host="127.0.0.1",
        port=settings.backend_port,
    )


if __name__ == "__main__":
    main()
