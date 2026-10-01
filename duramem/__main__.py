"""命令行入口：duramem <command>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from duramem import __version__
from duramem.config import get_settings
from duramem.models import Message
from duramem.service import DuramemError, Service


def _open_service(db: str | None = None, data_dir: str | None = None) -> Service:
    settings = _apply_data_dir(data_dir)
    if db:
        settings.default_db = db
    service = Service(settings)
    try:
        service.ensure_default_db()
    except Exception as exc:  # noqa: BLE001 - 指针失效不该让 CLI 直接崩
        print(f"[警告] {exc}", file=sys.stderr)
    return service


def _emit(payload: Any) -> None:
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(payload)


def _parse_setting_value(raw: str) -> Any:
    """`--set k=v` 的值解析。

    只认这三种，其余按字符串处理：`true` / `false` 转布尔，纯数字转 int。
    刻意不认 "null"——把字段清空应该用 `providers --reset k`，而不是塞一个空值。
    """
    text = raw.strip()
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if text and (text.lstrip("-").isdigit()):
        return int(text)
    return text


# ====================================================================== 命令


def _apply_data_dir(service_dir: str | None):
    """让 --data-dir 在服务构造前生效。

    hook 与 MCP 子进程的工作目录由宿主决定，不显式指定就会把库文件散进
    用户当时打开的项目目录里——所以这两条路径都必须能显式传数据目录。
    """
    from duramem.config import get_settings, reset_settings

    reset_settings()
    settings = get_settings()
    if service_dir:
        settings.data_dir = Path(service_dir).resolve()
    settings.ensure_dirs()
    return settings


def cmd_init(args: argparse.Namespace) -> int:
    # data_dir 必须传下去：不传的话 `duramem init --data-dir X` 会在**默认**
    # 数据目录建库并登记，而用户以为建在 X。实测踩过——测试用的临时目录被忽略，
    # 默认目录里凭空多出一个空库，还写进了 registry。
    service = _open_service(None, getattr(args, "data_dir", None))
    if args.db:
        try:
            service.create_database(args.db)
        except ValueError as exc:
            print(f"跳过：{exc}")
    _emit({"ok": True, "data_dir": str(service.settings.data_dir),
           "databases": [e.display_name for e in service.registry.list()]})
    service.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from duramem.api import create_app

    settings = _apply_data_dir(getattr(args, "data_dir", None))
    app = create_app(settings=settings)
    uvicorn.run(app, host=args.host, port=args.port or settings.backend_port)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from duramem import mcp_server

    argv = ["--transport", args.transport, "--host", args.host]
    if args.port:
        argv += ["--port", str(args.port)]
    if args.db:
        argv += ["--db", args.db]
    if getattr(args, "data_dir", None):
        argv += ["--data-dir", args.data_dir]
    return mcp_server.main(argv)


def cmd_ingest(args: argparse.Namespace) -> int:
    """从 JSON / JSONL 文件采集原始消息。

    这是采集适配器的手工入口：任何宿主只要能导出对话，就能喂进来。
    JSONL 每行一个对象；JSON 可以是数组或 {"messages": [...]}。
    """
    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2

    raw = path.read_text(encoding="utf-8")
    records: list[dict[str, Any]] = []
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        for line in raw.splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    else:
        loaded = json.loads(raw)
        records = loaded.get("messages", loaded) if isinstance(loaded, dict) else loaded

    messages = [
        Message(
            window_id=str(item.get("window_id", args.window)),
            session_id=str(item.get("session_id", args.session)),
            seq=int(item.get("seq", index)),
            role=str(item.get("role", "user")),
            content=str(item.get("content", "")),
            speaker=item.get("speaker"),
            ts=item.get("ts"),
            source_msg_id=item.get("source_msg_id"),
        )
        for index, item in enumerate(records)
        if str(item.get("content", "")).strip()
    ]

    if not messages:
        print("没有可导入的消息", file=sys.stderr)
        return 2

    service = _open_service(args.db, getattr(args, "data_dir", None))
    result = service.ingest(messages, db=args.db, auto_summarize=not args.no_summarize)

    if args.summarize:
        outgoing = service.summarize(messages[0].window_id, messages[0].session_id, db=args.db)
        result["summary"] = outgoing.to_payload()

    _emit(result)
    service.close()
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    service = _open_service(args.db, getattr(args, "data_dir", None))
    outcome = service.summarize(args.window, args.session, db=args.db)
    _emit(outcome.to_payload())
    service.close()
    return 0 if outcome.ok else 1


def cmd_search(args: argparse.Namespace) -> int:
    service = _open_service(args.db, getattr(args, "data_dir", None))
    try:
        if args.debug:
            _emit(service.debug_search(args.query, db=args.db, top_k=args.top_k))
        else:
            result = service.search(
                args.query, db=args.db, top_k=args.top_k, tags=args.tags or None
            )
            _emit(result.to_mcp_payload())
    except DuramemError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        service.close()
        return 1
    service.close()
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    service = _open_service(args.db, getattr(args, "data_dir", None))
    try:
        result = service.read_original(
            args.uid, db=args.db, mode=args.mode, window=args.window, detail=args.detail
        )
        _emit(result.to_payload())
    except DuramemError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        service.close()
        return 1
    service.close()
    return 0


def cmd_read_session(args: argparse.Namespace) -> int:
    """读取会话层的 L0/L1。"""
    service = _open_service(args.db, getattr(args, "data_dir", None))
    try:
        result = service.read_session(
            args.session_id, detail=args.detail, db=args.db, window_id=args.window_id
        )
        _emit(result.to_payload())
    except DuramemError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        service.close()
        return 1
    service.close()
    return 0


def cmd_summarize_session(args: argparse.Namespace) -> int:
    """强制生成/重生成某个会话的概览。

    与 `refresh-sessions` 的分工：这条**不看策略**，点名就生成，用于"我就是要
    现在刷新它"。批量走策略判断的那条见 `cmd_refresh_sessions`。
    """
    service = _open_service(args.db, getattr(args, "data_dir", None))
    try:
        outcome = service.summarize_session(args.window, args.session_id, db=args.db)
    except DuramemError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        service.close()
        return 1
    _emit(
        {
            "ok": outcome.ok,
            "window_id": outcome.window_id,
            "session_id": outcome.session_id,
            "model_used": outcome.model_used,
            "overview_tokens": outcome.overview_tokens,
            "abstract_tokens": outcome.abstract_tokens,
            "coverage_total": outcome.coverage_total,
            "coverage_sampled": outcome.coverage_sampled,
            "warnings": list(outcome.warnings),
        }
    )
    service.close()
    return 0 if outcome.ok else 1


def cmd_refresh_sessions(args: argparse.Namespace) -> int:
    """按 freshness 策略批量刷新会话概览。

    为什么是显式命令而不是自动：hook 是同步执行的（每轮已多约 0.9 秒），在采集
    路径上塞一次可能长达数十秒的模型调用会拖慢每一轮对话。
    """
    service = _open_service(args.db, getattr(args, "data_dir", None))
    _emit(service.refresh_sessions(db=args.db, limit=args.limit, force=args.force))
    service.close()
    return 0


def cmd_list_sessions(args: argparse.Namespace) -> int:
    service = _open_service(args.db, getattr(args, "data_dir", None))
    _emit({"sessions": service.list_session_layers(db=args.db, limit=args.limit)})
    service.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    service = _open_service(args.db, getattr(args, "data_dir", None))
    _emit(service.stats(args.db))
    service.close()
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    service = _open_service(args.db, getattr(args, "data_dir", None))
    _emit(service.reindex(args.db))
    service.close()
    return 0


def cmd_links_rebuild(args: argparse.Namespace) -> int:
    """整体重建被动链接（backfill）。

    新写入的切片在 insert/update 路径上自动建链；这条命令补的是
    "链接机制上线前就存在的存量库"。幂等，重复跑无副作用。
    """
    service = _open_service(args.db, getattr(args, "data_dir", None))
    _emit(service.rebuild_links(args.db))
    service.close()
    return 0


def cmd_rebuild_vectors(args: argparse.Namespace) -> int:
    """重建向量表并全量重新嵌入。

    换嵌入模型、或改 VECTOR_CHUNK_SIZE 之后用这个——两者都无法原地修改，
    只能删表重建。模型一致性校验会在这条路径上被有意绕过（它的目的就是修正不一致）。
    """
    service = _open_service(args.db, getattr(args, "data_dir", None))
    try:
        _emit(service.rebuild_vectors(args.db))
    except KeyError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        service.close()
        return 1
    service.close()
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    """查看或修改模型配置（无界面时的等价入口）。

    改动语义与 REST 完全一致：走 Service，所以"换模型会作废已有向量"
    这件事在这里也会被记下来并打印出来。
    """
    service = _open_service(args.db, getattr(args, "data_dir", None))
    try:
        if args.set:
            patch: dict[str, object] = {}
            for item in args.set:
                if "=" not in item:
                    print(f"错误：--set 需要 key=value，收到 {item!r}", file=sys.stderr)
                    return 2
                key, _, raw = item.partition("=")
                patch[key.strip()] = _parse_setting_value(raw)

            result = service.update_provider_config(patch)
            if result["rejected"]:
                for key, reason in result["rejected"].items():
                    print(f"拒绝 {key}：{reason}", file=sys.stderr)
            if result["identity_changed"]:
                marked = result["vectors_marked_stale"]
                print(
                    f"嵌入提供方：{result['previous_provider']} → "
                    f"{result['effective_embedding']['provider']}",
                    file=sys.stderr,
                )
                if marked:
                    print(
                        f"这些库的向量已作废，重建前检索会被拒绝：{'、'.join(marked)}\n"
                        f"执行：duramem rebuild-vectors --db <库名>",
                        file=sys.stderr,
                    )
            if not result["applied"] and result["rejected"]:
                return 1

        if args.reset:
            service.reset_provider_config(None if args.reset == ["all"] else args.reset)
            print("已清除这些字段的界面覆盖，回落到 .env 与默认值", file=sys.stderr)

        view = service.provider_view()
        _emit(
            {
                "effective_embedding": view["effective_embedding"],
                "rerank": view["rerank"],
                "summary": view["summary"],
                "databases": view["databases"],
                "overrides": view["overrides"],
                "overrides_file": view["overrides_file"],
            }
        )
        return 0
    finally:
        service.close()


def cmd_export(args: argparse.Namespace) -> int:
    """导出为人类可读的 JSON。

    "一个文件 = 一份记忆"是核心卖点，导出成可读格式让记忆能被直接查看与分享。
    向量不在归档里（可由当前提供方重建），会话概览必须在（那是花模型调用生成的）。
    """
    service = _open_service(None, getattr(args, "data_dir", None))
    payload = service.export_database(db=args.db, with_messages=args.with_messages)
    name = str(payload["db"])

    target = Path(args.out or f"{name}-export.json")
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _emit({"ok": True, "path": str(target), "chunks": len(payload.get("chunks", [])),
           "messages": len(payload.get("messages", [])),
           "layers": len(payload.get("layers", []))})
    service.close()
    return 0


def cmd_import_archive(args: argparse.Namespace) -> int:
    """把 export 生成的归档还原成一个新库（导出/导入的"入"那一半）。

    只进新库不合并：归档里的 uid / 消息 id / 游标是一套互相咬合的寻址体系，
    往已有库里合并等于把它们搅乱。目标名冲突时后端会拒绝，这里不抢先拦截——
    让统一的错误文案去解释。
    """
    service = _open_service(None, getattr(args, "data_dir", None))
    try:
        result = service.import_archive(
            path=args.path,
            db=args.db,
            dry_run=args.dry_run,
            reindex=not args.no_reindex,
        )
    finally:
        service.close()
    _emit(result)
    return 0


# ====================================================================== 解析


def cmd_hook_ingest(args: argparse.Namespace) -> int:
    """宿主 hook 的采集入口：从 stdin 读载荷并入库。

    stdout **只允许**输出合法 JSON 或什么都不输出——ZCode 的 hook 规范要求如此，
    多余输出会让该次 hook 运行被判为失败。所以这里把日志都写到 stderr。
    退出码恒为 0：采集失败不该阻塞用户的会话。
    """
    from duramem.hooks import run_hook

    data_dir = Path(args.data_dir).resolve() if args.data_dir else get_settings().data_dir
    payload = run_hook(
        event=args.event,
        data_dir=data_dir,
        db=args.db,
        window_id=args.window,
        session_id=args.session,
    )
    if args.verbose:
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """把已有对话导入记忆库。

    三条用法：
    - `duramem import --list`：列出可选的对话（带序号），先看再挑
    - `duramem import --index 3 --index 7`：导入上面列出的第 3、7 个
    - `duramem import --session <id>`：按会话 id 精确导入
    不传路径时用该源的默认位置，所以不需要先找绝对路径。
    """
    service = _open_service(args.db, getattr(args, "data_dir", None))

    since_days = None
    since_ms = None
    if args.since:
        from datetime import datetime, timedelta, timezone

        try:
            since_days = int(args.since)
            since_ms = int(
                (datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp() * 1000
            )
        except ValueError:
            print(f"--since 需要是天数，收到 {args.since!r}", file=sys.stderr)
            service.close()
            return 2

    # ---- 只列不导 ----
    if args.list:
        try:
            listing = service.list_importable_conversations(
                path=args.path,
                source=args.source,
                db=args.db,
                include_subagents=args.include_subagents,
                since_days=since_days,
                refresh=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"错误：{exc}", file=sys.stderr)
            service.close()
            return 2
        items = listing["conversations"]
        print(f"来源：{listing['source']}    路径：{listing['path']}")
        print(f"共 {listing['total']} 个对话（不含子代理会话）" if not args.include_subagents
              else f"共 {listing['total']} 个对话（含子代理会话）")
        print()
        print(f"  {'#':>3}  {'消息':>5}  {'日期':<11} {'窗口':<20} 标题")
        print("  " + "-" * 96)
        for index, item in enumerate(items, start=1):
            date = (item["created_at"] or "")[:10]
            window = str(item["window_id"])[:20]
            mark = " ✓已导入" if item["already_imported"] else ""
            title = item["title"][:44]
            print(f"  {index:>3}  {item['messages']:>5}  {date:<11} {window:<20} {title}{mark}")
            print(f"       id: {item['session_id']}")
            print(f"       {item['first_line'][:96]}")
        print()
        print("挑好之后用 --index 导入，例如：duramem import --index 3 --summarize")
        service.close()
        return 0

    # ---- 按序号选（列表里的 #）----
    sessions = list(args.session or [])
    if args.index:
        try:
            listing = service.list_importable_conversations(
                path=args.path,
                source=args.source,
                db=args.db,
                include_subagents=args.include_subagents,
                since_days=since_days,
                refresh=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"错误：{exc}", file=sys.stderr)
            service.close()
            return 2
        items = listing["conversations"]
        for raw in args.index:
            if not (1 <= raw <= len(items)):
                print(f"序号越界：{raw}（本次列表共 {len(items)} 个）", file=sys.stderr)
                service.close()
                return 2
            sessions.append(items[raw - 1]["session_id"])

    if not sessions and not args.all:
        print(
            "需要指定要导入哪些对话：用 --list 看列表，然后 --index N 或 --session <id>；\n"
            "确实想导入该源的全部对话，加 --all。",
            file=sys.stderr,
        )
        service.close()
        return 2

    try:
        report = service.import_conversations(
            path=args.path,
            source=args.source,
            db=args.db,
            dry_run=args.dry_run,
            include_subagents=args.include_subagents,
            sessions=sessions or None,
            since_ms=since_ms,
            limit=args.limit,
            min_messages=args.min_messages,
            summarize=args.summarize,
            max_messages=args.max_messages,
        )
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        service.close()
        return 2

    if args.dry_run and report.preview:
        print(f"来源：{report.source}")
        print(
            f"选中 {report.conversations_found} 个对话，可导入 {len(report.preview)} 个"
            f"（跳过 {report.skipped_short} 个过短）"
        )
        print()
        for item in report.preview[:40]:
            print(f"  {item['messages']:>5} 条  {item['title'][:50]}")
        print()
        print("这是预览，没有写入任何数据。")
    else:
        _emit(report.to_payload())
    service.close()
    return 0 if not report.errors else 1


def cmd_sources(args: argparse.Namespace) -> int:
    """列出本机可导入的默认位置。只读探测，不碰数据库。"""
    from duramem.importers import known_defaults, list_sources

    _emit({"sources": list_sources(), "defaults": known_defaults()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="duramem",
        description="Duramem —— 挂载式 LLM 记忆服务：切片即记忆，同时作为原文索引",
    )
    parser.add_argument("--version", action="version", version=f"duramem {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化数据目录与库")
    p.add_argument("--db", help="额外创建指定名字的库")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="启动 REST + 前端后端服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mcp", help="启动 MCP 服务")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--db", help="挂载哪个库（stdio 模式下的库选择方式）")
    p.add_argument("--data-dir", help="数据目录的绝对路径（强烈建议显式指定）")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("ingest", help="从 JSON/JSONL 采集原始消息")
    p.add_argument("file")
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.add_argument("--window", default="cli-window")
    p.add_argument("--session", default="cli-session")
    p.add_argument("--summarize", action="store_true", help="采集后立即总结")
    p.add_argument("--no-summarize", action="store_true", help="即使开了自动总结也不触发")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("summarize", help="对某个会话触发一次总结")
    p.add_argument("window")
    p.add_argument("session")
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.set_defaults(func=cmd_summarize)

    p = sub.add_parser("search", help="检索记忆")
    p.add_argument("query")
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.add_argument("--top-k", type=int, default=None)
    p.add_argument("--tags", action="append")
    p.add_argument("--debug", action="store_true", help="输出三路排名的调试数据")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("read", help="回查切片原文")
    p.add_argument("uid")
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.add_argument("--mode", choices=["quote", "window", "full"], default="quote")
    p.add_argument("--window", type=int, default=1)
    p.add_argument("--detail", choices=["minimal", "standard", "full"], default="standard")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("read-session", help="读取会话概览（L0/L1）")
    p.add_argument("session_id")
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.add_argument("--window-id", help="会话所属窗口；不传则按 session_id 取最近一份")
    p.add_argument("--detail", choices=["abstract", "overview"], default="overview")
    p.set_defaults(func=cmd_read_session)

    p = sub.add_parser("summarize-session", help="强制生成某个会话的概览")
    p.add_argument("window")
    p.add_argument("session_id")
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.set_defaults(func=cmd_summarize_session)

    p = sub.add_parser(
        "refresh-sessions",
        help="按 freshness 策略批量刷新会话概览（不做自动触发，见设计文档）",
    )
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.add_argument("--limit", type=int, default=0, help="最多处理几个候选；0 = 不限")
    p.add_argument("--force", action="store_true", help="绕过策略，全部重生成")
    p.set_defaults(func=cmd_refresh_sessions)

    p = sub.add_parser("sessions", help="列出已生成的会话概览")
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_list_sessions)

    p = sub.add_parser("stats", help="查看库状态")
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("reindex", help="全量重建向量索引")
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser(
        "links-rebuild",
        help="整体重建切片间的被动链接（存量库 backfill；新切片自动建链）",
    )
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.set_defaults(func=cmd_links_rebuild)

    p = sub.add_parser(
        "rebuild-vectors",
        help="删除并重建向量表后全量重新嵌入（换嵌入模型或改 VECTOR_CHUNK_SIZE 后用）",
    )
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.set_defaults(func=cmd_rebuild_vectors)

    p = sub.add_parser(
        "providers",
        help="查看/修改模型配置（界面「模型」页的等价入口）",
    )
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.add_argument(
        "--set",
        action="append",
        metavar="key=value",
        help="改一项，可重复。例：--set embedding_model=BAAI/bge-m3 --set embedding_api_key=sk-xxx",
    )
    p.add_argument(
        "--reset",
        nargs="*",
        metavar="key",
        help="清除覆盖回落到 .env；不带参数或传 all 表示全部清除",
    )
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser("export", help="导出为可读 JSON")
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.add_argument("--out")
    p.add_argument("--with-messages", action="store_true", help="同时导出原始消息")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser(
        "import-archive",
        help="把 export 生成的归档还原成一个新库（导出/导入往返）",
    )
    p.add_argument("path", help="归档 JSON 路径（duramem export 的产物）")
    p.add_argument("--db", help="目标库名；不传则用归档里的 db 名")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.add_argument("--dry-run", action="store_true", help="只检查与预览，不写任何数据")
    p.add_argument(
        "--no-reindex",
        action="store_true",
        help="跳过向量重建（导入后检索只有词法路，需手动 rebuild-vectors）",
    )
    p.set_defaults(func=cmd_import_archive)

    p = sub.add_parser(
        "import",
        help="把已有对话导入记忆库（不传路径时用该源的默认位置）",
    )
    p.add_argument("path", nargs="?", help="文件或目录；不传则用该源的默认位置")
    p.add_argument(
        "--source",
        choices=["zcode", "rollout", "claude", "generic"],
        help="显式指定源；不指定则自动识别",
    )
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.add_argument("--list", action="store_true", help="只列出可选的对话，不导入")
    p.add_argument(
        "--index",
        type=int,
        action="append",
        help="导入列表里的第 N 个（配合 --list 使用），可重复",
    )
    p.add_argument("--dry-run", action="store_true", help="只预览不写库")
    p.add_argument(
        "--all",
        action="store_true",
        help="导入该源的全部对话（默认必须显式选，避免一次灌进整个历史）",
    )
    p.add_argument(
        "--include-subagents",
        action="store_true",
        help="连同子代理会话一起列出/导入（默认排除：那是主代理派出去的任务，不是你的对话）",
    )
    p.add_argument("--session", action="append", help="只导入指定会话 id，可重复")
    p.add_argument("--since", help="只列出/导入最近 N 天")
    p.add_argument("--limit", type=int, help="最多处理多少个对话")
    p.add_argument(
        "--max-messages",
        type=int,
        help="每个对话只取最近 N 条消息（大对话用得上）",
    )
    p.add_argument(
        "--min-messages",
        type=int,
        default=2,
        help="少于这么多条消息的对话跳过（默认 2）",
    )
    p.add_argument("--summarize", action="store_true", help="导入后立刻切成记忆切片")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("sources", help="列出本机可导入的默认位置")
    p.add_argument("--data-dir", help="数据目录的绝对路径")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser(
        "hook-ingest",
        help="宿主 hook 的采集入口（从 stdin 读 JSON 载荷并入库）",
    )
    p.add_argument("--event", required=True, help="事件名，如 UserPromptSubmit / Stop")
    p.add_argument("--db")
    p.add_argument("--data-dir", help="数据目录的绝对路径（hook 里建议显式传绝对路径）")
    p.add_argument("--window", help="覆盖窗口标识；默认取载荷里的 cwd")
    p.add_argument("--session", help="覆盖会话标识；默认取载荷里的 session_id")
    p.add_argument("--verbose", action="store_true", help="把结果打到 stderr 便于排查")
    p.set_defaults(func=cmd_hook_ingest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
