"""导入器注册与调度。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from duramem.importers.base import (
    Conversation,
    ConversationSummary,
    Importer,
    ImportReport,
    normalize_role,
    survey_source,
)
from duramem.importers.dsh import DshSessionImporter, default_dsh_dir
from duramem.importers.transcripts import (
    ClaudeCodeImporter,
    GenericJsonImporter,
    ZCodeRolloutImporter,
    default_claude_dir,
    default_zcode_rollout_dir,
)
from duramem.importers.zcode_db import ZCodeDbImporter, default_db_path

# 顺序即优先级。通用适配器必须排在最后——它总能解析出点什么，
# 放前面会把 ZCode / Claude Code 的转录按通用规则误读。
IMPORTERS: tuple[Importer, ...] = (
    ZCodeDbImporter(),
    ZCodeRolloutImporter(),
    ClaudeCodeImporter(),
    DshSessionImporter(),
    GenericJsonImporter(),
)

SOURCE_ALIASES = {
    "zcode": "zcode-db",
    "zcode-db": "zcode-db",
    "rollout": "zcode-rollout",
    "zcode-rollout": "zcode-rollout",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "dsh": "dsh",
    "generic": "generic",
    "json": "generic",
}


def list_sources() -> list[dict[str, str]]:
    return [
        {"name": item.name, "description": item.description} for item in IMPORTERS
    ]


def get_importer(name: str) -> Importer:
    resolved = SOURCE_ALIASES.get(name, name)
    for item in IMPORTERS:
        if item.name == resolved:
            return item
    raise ValueError(
        f"未知的导入源：{name}；可用：{', '.join(sorted(SOURCE_ALIASES))}"
    )


def detect_importer(path: Path) -> Importer | None:
    """按优先级找第一个认得的适配器。认不出来就返回 None。"""
    for item in IMPORTERS:
        try:
            if item.probe(path):
                return item
        except Exception:  # noqa: BLE001 - 探测失败就换下一个，不该因此中断
            continue
    return None


def known_defaults() -> list[dict[str, object]]:
    """本机上可一键导入的默认位置。给用户看"能导什么"，免得自己找路径。"""
    out: list[dict[str, object]] = []

    zcode_db = default_db_path()
    out.append(
        {
            "source": "zcode-db",
            "path": str(zcode_db),
            "exists": zcode_db.exists(),
            "label": "ZCode 会话库",
            "hint": "你在这个客户端里的历史对话（默认排除子代理会话）",
        }
    )

    rollout = default_zcode_rollout_dir()
    count = len(list(rollout.glob("model-io-*.jsonl"))) if rollout.is_dir() else 0
    out.append(
        {
            "source": "zcode-rollout",
            "path": str(rollout),
            "exists": rollout.is_dir(),
            "label": "ZCode 模型往返日志",
            "hint": f"共 {count} 个文件。会话库不可用时的备选源",
        }
    )

    claude = default_claude_dir()
    files = len(list(claude.glob("**/*.jsonl"))) if claude.is_dir() else 0
    out.append(
        {
            "source": "claude-code",
            "path": str(claude),
            "exists": claude.is_dir(),
            "label": "Claude Code 转录",
            "hint": f"共 {files} 个文件",
        }
    )

    dsh = default_dsh_dir()
    dsh_count = len(list(dsh.glob("**/session-*/session.*.jsonl*"))) if dsh.is_dir() else 0
    out.append(
        {
            "source": "dsh",
            "path": str(dsh),
            "exists": dsh.is_dir(),
            "label": "DeepSeek Harness 会话",
            "hint": f"共 {dsh_count} 个会话文件",
        }
    )

    return out


def resolve_path(raw: str | None, source: str | None) -> tuple[Path, str]:
    """把用户给的路径/源名解析成一个具体的输入路径与源名。

    不传路径时用该源的默认位置——"导入我的历史"不该要求用户先找到文件的绝对路径。
    """
    if raw:
        path = Path(raw).expanduser()
    else:
        name = SOURCE_ALIASES.get(source or "zcode", source or "zcode")
        defaults = {
            "zcode-db": default_db_path(),
            "zcode-rollout": default_zcode_rollout_dir(),
            "claude-code": default_claude_dir(),
            "dsh": default_dsh_dir(),
        }
        if name not in defaults:
            raise ValueError(
                f"源 {source} 需要显式提供路径（没有约定俗成的默认位置）"
            )
        path = defaults[name]
    return path, (source or "")


def build_report(
    source: str,
    conversations: Sequence[Conversation],
    dry_run: bool,
    min_messages: int = 2,
) -> ImportReport:
    """只扫描不写库，用于 --dry-run 预览。"""
    report = ImportReport(source=source, dry_run=True)
    for item in conversations:
        report.conversations_found += 1
        if not item.messages:
            report.skipped_empty += 1
            continue
        if len(item.messages) < min_messages:
            report.skipped_short += 1
            continue
        report.preview.append(
            {
                "origin_id": item.origin_id,
                "title": item.title,
                "window_id": item.window_id,
                "session_id": item.session_id,
                "messages": len(item.messages),
                "created_at": item.created_at,
                "is_subagent": bool(item.metadata.get("is_subagent")),
                "first_line": item.messages[0].content.splitlines()[0][:90]
                if item.messages
                else "",
            }
        )
    if not dry_run:
        report.dry_run = False
    return report


def list_available(
    path: str | None = None,
    source: str | None = None,
    include_subagents: bool = False,
    since_ms: int | None = None,
    imported_ids: set[str] | None = None,
) -> tuple[str, str, list[ConversationSummary]]:
    """列出一个源里可导入的对话。

    返回 `(源名, 实际路径, 对话列表)`。不写库——用户要先看到有什么、再挑。
    """
    target, raw_source = resolve_path(path, source)
    if not target.exists():
        raise ValueError(f"路径不存在：{target}")

    importer = get_importer(raw_source) if raw_source else detect_importer(target)
    if importer is None:
        raise ValueError(
            f"无法识别该路径的格式：{target}。可用 --source 显式指定"
            "（zcode / rollout / claude / generic）。"
        )

    items = survey_source(
        importer,
        target,
        include_subagents=include_subagents,
        since_ms=since_ms,
        imported_ids=imported_ids,
    )
    return importer.name, str(target), items


__all__ = [
    "IMPORTERS",
    "SOURCE_ALIASES",
    "build_report",
    "detect_importer",
    "get_importer",
    "known_defaults",
    "list_available",
    "list_sources",
    "normalize_role",
    "resolve_path",
]
