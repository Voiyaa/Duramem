"""原文回查（L2）：mode / detail / 取回量报告。

设计文档 §7。三条约束：

1. **不设硬上限**。v1 写的"回查限制无"曾被改成"每轮累计预算"，理由是会一次灌进
   数万 token、把宿主推向它自己的压缩。那条理由**已被撤销**，改照 OpenViking:
   它对 L2 读取不设上限，体量靠**定位精度**（精确 uid + msg 区间）控制，而不是
   靠限额。实测切片原文 3200–8000 token，OvK 式的读法是"读 1–3 条具体的"，
   对现代上下文微不足道；而限额造成了真实伤害——一条 7956 token 的区间被拒后
   只能退到 500 token 的摘要，丢 94%。
   现在只做**累计报告**：返回体报告本轮已取回多少，让模型自己看得见。
   超出一个可选的软阈值（默认不设）时给一句提示，但**不拒绝**。
2. **有粒度**。`quote` 只给精确区间，`window` 带相邻消息，`full` 才给整块。
   整块回捞会让模型拿回一堆还得自己再找一遍的文本。
3. **量要明说**，不能让模型以为"没提到就是不存在"。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from duramem.config import Settings
from duramem.models import render_messages_with_offsets
from duramem.store.repository import Repository
from duramem.text.tokenize import estimate_tokens

MODE_QUOTE = "quote"
MODE_WINDOW = "window"
MODE_FULL = "full"
_MODES = {MODE_QUOTE, MODE_WINDOW, MODE_FULL}

DETAIL_MINIMAL = "minimal"
DETAIL_STANDARD = "standard"
DETAIL_FULL = "full"
_DETAILS = {DETAIL_MINIMAL, DETAIL_STANDARD, DETAIL_FULL}

MINIMAL_TOKEN_CAP = 500

# 会话层的 detail 档位。默认给概览——模型既然点名要这个会话，就是要它的脉络。
SESSION_DETAIL_OVERVIEW = "overview"
SESSION_DETAIL_ABSTRACT = "abstract"
_SESSION_DETAILS = {SESSION_DETAIL_OVERVIEW, SESSION_DETAIL_ABSTRACT}

# 句末标点。截断优先落在这些字符之后，避免给出半句话。
_SENTENCE_ENDS = "。！？!?；;\n"
# 次要切点：子句分隔符。中文句子动辄三四十字只用一个逗号，只认句末的话
# 常常找不到落点，于是退回原始切点、切在词中间（实测切成「定位到端口占」）。
# 对"我只要个大概"这种请求，退到逗号远好过半个词。
_CLAUSE_ENDS = "，,、：:"


def truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    """按 token 预算截断，尽量截在标点处。返回 (文本, 是否被截断)。

    为什么要在标点处截：按字符二分收敛会切在词中间——实测把 `parse-transcript`
    切成了 `parse-transcri`。截断的语义是"少给一点"，不该给出半句话：那比少给
    更难用，因为读者分不清那是原文的样子还是被切过。

    三级退让，先试代价小的：
      1. 小幅回退找**句末**（最多丢四分之一）
      2. 放宽回退范围找**任意断点**（句末或子句分隔符，最多丢一半）
      3. 都不成则用原始切点

    第 2 级是必需的：中文长句常常三四十字只用一个逗号，只认句末且回退范围
    太窄的话找不到落点，就退回原始切点、切在词中间（实测切成「定位到端口占」）。
    """
    if max_tokens <= 0:
        return "", bool(text)
    if estimate_tokens(text) <= max_tokens:
        return text, False

    # 先按字符比例粗估，再二分收敛。估算函数是单调的，二分一定收敛。
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    head = text[:low]

    for marks, floor in (
        (_SENTENCE_ENDS, low * 3 // 4),
        (_SENTENCE_ENDS + _CLAUSE_ENDS, low // 2),
    ):
        cut = _rfind_break(head, marks, floor=floor)
        if cut is not None:
            return head[: cut + 1], True

    return head, True


def _rfind_break(head: str, marks: str, floor: int) -> int | None:
    """从 head 末尾往回找最后一个断点字符，找不到返回 None。"""
    for index in range(len(head) - 1, max(floor, 0) - 1, -1):
        char = head[index]
        if char not in marks:
            continue
        # 句号后面紧跟数字的话那是小数点，不是句末（`x 1.5` 不该切在 `1.` 之后）
        if char == "." and index + 1 < len(head) and head[index + 1].isdigit():
            continue
        return index
    return None


@dataclass
class ReadResult:
    ok: bool
    text: str = ""
    mode: str = ""
    detail: str = ""
    tokens: int = 0
    truncated: bool = False
    tokens_this_round: int = 0
    uid: str = ""
    title: str = ""
    msg_id_start: int | None = None
    msg_id_end: int | None = None
    warnings: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "uid": self.uid,
            "title": self.title,
            "mode": self.mode,
            "detail": self.detail,
            "tokens": self.tokens,
            "tokens_this_round": self.tokens_this_round,
            "original": self.text,
        }
        if self.truncated:
            payload["truncated"] = True
        if self.msg_id_start is not None:
            payload["msg_range"] = [self.msg_id_start, self.msg_id_end]
        if self.warnings:
            payload["warnings"] = self.warnings
        return payload


@dataclass
class SessionReadResult:
    """会话层（L0/L1）的读取结果。

    与会话的检索命中刻意分开：命中只带 L0（256 token，便宜），这里才给 L1
    （≤4000 token）。照 OpenViking 的分工，概览是**按需取的**，不是随每次检索
    自动注入的——那会在命中跨多个会话时把每轮上下文灌爆。
    """

    ok: bool
    session_id: str = ""
    window_id: str = ""
    abstract_text: str = ""
    overview: str = ""
    detail: str = ""
    slice_count: int = 0
    model_used: str | None = None
    updated_at: str = ""
    tokens: int = 0
    tokens_this_round: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "session_id": self.session_id,
            "abstract": self.abstract_text,
            "detail": self.detail,
            "slice_count": self.slice_count,
            "tokens": self.tokens,
            "tokens_this_round": self.tokens_this_round,
        }
        if self.window_id:
            payload["window_id"] = self.window_id
        if self.overview:
            payload["overview"] = self.overview
        if self.model_used:
            payload["model_used"] = self.model_used
        if self.updated_at:
            payload["updated_at"] = self.updated_at
        if self.warnings:
            payload["warnings"] = self.warnings
        return payload


class ReadUsageTracker:
    """本轮回查已经取回多少 token。

    早先这里是个**限额器**：每轮累计到 4000 就拒绝后续回查。那个设计被撤销了——
    照 OpenViking，L2 读取不设上限，体量由"定位精度"控制而不是限额。

    现在它只做三件事：累计、报告、在超过**可选**软阈值时给一句提示。
    `soft_limit_tokens = 0`（默认）表示不设阈值，也就是完全照 OvK：读多少给多少。

    "一轮"的界定沿用原设计：新的检索调用（`begin_round`）重置计数；
    空闲超过 `idle_reset` 秒也算新一轮——MCP 协议里没有天然的轮次概念。
    """

    def __init__(self, soft_limit_tokens: int = 0, idle_reset_seconds: int = 120) -> None:
        self.soft_limit_tokens = max(0, soft_limit_tokens)
        self.idle_reset_seconds = max(0, idle_reset_seconds)
        self._lock = threading.Lock()
        self._used: dict[str, int] = {}
        self._touched: dict[str, float] = {}

    def _key(self, db_name: str, session: str) -> str:
        return f"{db_name}::{session}"

    def _maybe_expire(self, key: str) -> None:
        last = self._touched.get(key)
        if last is not None and time.monotonic() - last >= self.idle_reset_seconds:
            self._used.pop(key, None)

    def begin_round(self, db_name: str, session: str) -> None:
        """开始新一轮：计数归零。由检索入口调用（见 Service.search）。"""
        with self._lock:
            key = self._key(db_name, session)
            self._used[key] = 0
            self._touched[key] = time.monotonic()

    def used(self, db_name: str, session: str) -> int:
        """本轮已取回量。"""
        with self._lock:
            key = self._key(db_name, session)
            self._maybe_expire(key)
            return max(0, self._used.get(key, 0))

    def record(self, db_name: str, session: str, tokens: int) -> int:
        """记一笔取回量，返回本轮累计。不做判断、不拒绝。"""
        with self._lock:
            key = self._key(db_name, session)
            self._maybe_expire(key)
            self._used[key] = self._used.get(key, 0) + max(0, int(tokens))
            self._touched[key] = time.monotonic()
            return self._used[key]

    def over_soft_limit(self, db_name: str, session: str) -> bool:
        """本轮是否超过软阈值。未设阈值时恒为 False。"""
        if not self.soft_limit_tokens:
            return False
        return self.used(db_name, session) > self.soft_limit_tokens


class OriginalReader:
    """记忆三层的读取：L2 原文（按切片）与会话层 L0/L1（按会话）。"""

    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        usage: ReadUsageTracker,
        db_name: str = "",
        session_layers: Any = None,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.usage = usage
        self.db_name = db_name
        # 会话层是可选依赖：没有它时 `read_session` 会明确报错而不是崩掉，
        # 这样只关心切片的应用不必构造会话层。
        self.session_layers = session_layers

    # ------------------------------------------------------------------

    def read(
        self,
        uid: str,
        mode: str = MODE_QUOTE,
        window: int | None = None,
        detail: str = DETAIL_STANDARD,
        session: str = "default",
    ) -> ReadResult:
        if not self.settings.original_access_enabled:
            return ReadResult(
                ok=False,
                uid=uid,
                warnings=[
                    "本服务已关闭 L2 原文回查（设置项 ORIGINAL_ACCESS_ENABLED），"
                    "只能返回记忆摘要。"
                ],
                tokens_this_round=self.usage.used(self.db_name, session),
            )

        mode = (mode or MODE_QUOTE).lower()
        detail = (detail or DETAIL_STANDARD).lower()
        if mode not in _MODES:
            return ReadResult(
                ok=False,
                uid=uid,
                warnings=[f"不支持的 mode：{mode}，可用 {sorted(_MODES)}"],
                tokens_this_round=self.usage.used(self.db_name, session),
            )
        if detail not in _DETAILS:
            return ReadResult(
                ok=False,
                uid=uid,
                warnings=[f"不支持的 detail：{detail}，可用 {sorted(_DETAILS)}"],
                tokens_this_round=self.usage.used(self.db_name, session),
            )

        chunk = self.repo.get_chunk(uid)
        if chunk is None:
            return ReadResult(
                ok=False,
                uid=uid,
                warnings=[f"切片不存在或已删除：{uid}"],
                tokens_this_round=self.usage.used(self.db_name, session),
            )

        warnings: list[str] = []
        text = self._render(chunk, mode, window, warnings)
        if not text:
            return ReadResult(
                ok=False,
                uid=uid,
                title=chunk["title"] or "",
                warnings=warnings or ["该切片没有可回查的原文"],
                tokens_this_round=self.usage.used(self.db_name, session),
            )

        # detail=minimal 做一次分寸截断（截在句末）。这不是限额，是"我只想要个大概"
        # 的显式请求——所以它截断而不是拒绝。
        if detail == DETAIL_MINIMAL:
            text, truncated = truncate_to_tokens(text, MINIMAL_TOKEN_CAP)
            if truncated:
                warnings.append(
                    f"内容已截断至约 {MINIMAL_TOKEN_CAP} token（截在句末）；"
                    "需要完整内容请提高 detail 参数。"
                )

        tokens = estimate_tokens(text)
        # 照 OpenViking：不设读取上限，读多少给多少。这里只记账与报告——
        # 让模型自己看得见本轮花了多少，而不是被一个阈值拦住。
        used = self.usage.record(self.db_name, session, tokens)
        if self.usage.over_soft_limit(self.db_name, session):
            warnings.append(
                f"本轮已取回约 {used} token，超过软阈值 "
                f"{self.usage.soft_limit_tokens}——仅提示，不影响本次返回。"
            )
        if detail != DETAIL_MINIMAL:
            truncated = False

        return ReadResult(
            ok=True,
            text=text,
            mode=mode,
            detail=detail,
            tokens=tokens,
            truncated=bool(truncated),
            tokens_this_round=used,
            uid=uid,
            title=chunk["title"] or "",
            msg_id_start=chunk["msg_id_start"],
            msg_id_end=chunk["msg_id_end"],
            warnings=warnings,
        )

    # ------------------------------------------------------------------

    def _render(
        self, chunk: dict[str, Any], mode: str, window: int | None, warnings: list[str]
    ) -> str:
        if mode == MODE_FULL:
            return chunk["original_text"] or ""

        start, end = chunk["msg_id_start"], chunk["msg_id_end"]
        if start is None or end is None:
            warnings.append("该切片没有关联的消息区间，已返回完整原文。")
            return chunk["original_text"] or ""

        if mode == MODE_QUOTE:
            # 优先用精确字符区间；缺失时退回整个消息区间
            c_start, c_end = chunk["original_char_start"], chunk["original_char_end"]
            original = chunk["original_text"] or ""
            if (
                c_start is not None
                and c_end is not None
                and 0 <= c_start < c_end <= len(original)
            ):
                return original[c_start:c_end]
            warnings.append("缺少精确字符区间，已按消息区间返回。")
            mode = MODE_WINDOW
            window = 0 if window is None else window

        expand = self.settings.default_window_expand if window is None else max(0, int(window))
        if expand > 0:
            # 带上切片所属的窗口/会话：messages.id 是全库自增的，不加这个边界，
            # 扩展会越过会话边缘取到别的对话的消息。
            rows = self.repo.expand_range(
                int(start),
                int(end),
                expand,
                expand,
                window_id=chunk.get("source_window"),
                session_id=chunk.get("source_session"),
            )
        else:
            rows = self.repo.get_message_range(
                int(start), int(end), conversational_only=True
            )

        if not rows:
            warnings.append("关联消息已被清理，已退回存入时的原文快照。")
            return chunk["original_text"] or ""
        return render_messages_with_offsets(rows)[0]

    # ------------------------------------------------------------------ 会话层

    def read_session(
        self,
        session_id: str,
        detail: str = SESSION_DETAIL_OVERVIEW,
        session: str = "default",
        window_id: str | None = None,
    ) -> SessionReadResult:
        """读取会话层的 L0 摘要与 L1 概览。

        `detail`:
        - `overview`（默认）：L0 + L1 概览。模型点名要这个会话时就是要它的脉络。
        - `abstract`：只给简介（≤256 token）。用于"先看看这个会话跟我有关吗"这种
          便宜的探路——模型从切片命中拿到 `session_id` 时手里没有摘要，
          直接拉 4000 token 的概览可能白花。

        `window_id` 可选：模型通常只有 session_id（检索命中回传的），没有 window。
        """
        if not self.settings.overview_access_enabled:
            return SessionReadResult(
                ok=False,
                session_id=session_id,
                warnings=[
                    "本服务已关闭会话概览（设置项 OVERVIEW_ACCESS_ENABLED），"
                    "只能返回切片级摘要。"
                ],
                tokens_this_round=self.usage.used(self.db_name, session),
            )

        detail = (detail or SESSION_DETAIL_OVERVIEW).lower()
        if detail not in _SESSION_DETAILS:
            return SessionReadResult(
                ok=False,
                session_id=session_id,
                warnings=[f"不支持的 detail：{detail}，可用 {sorted(_SESSION_DETAILS)}"],
                tokens_this_round=self.usage.used(self.db_name, session),
            )

        if self.session_layers is None:
            return SessionReadResult(
                ok=False,
                session_id=session_id,
                warnings=["该库未启用会话层。"],
                tokens_this_round=self.usage.used(self.db_name, session),
            )

        row = (
            self.session_layers.get(window_id, session_id)
            if window_id is not None
            else self.session_layers.get_by_session(session_id)
        )
        if row is None:
            return SessionReadResult(
                ok=False,
                session_id=session_id,
                warnings=[
                    f"没有该会话的概览：{session_id}。"
                    "可能是还没生成（见 refresh_sessions），或者会话标识不对。"
                ],
                tokens_this_round=self.usage.used(self.db_name, session),
            )

        abstract = str(row.get("abstract_text") or "")
        overview = str(row.get("overview_text") or "") if detail == SESSION_DETAIL_OVERVIEW else ""
        warnings: list[str] = []
        if detail == SESSION_DETAIL_OVERVIEW and not overview:
            warnings.append("该会话还没有概览正文，只返回了摘要。")

        text = f"{abstract}\n\n{overview}".strip() if overview else abstract
        tokens = estimate_tokens(text)
        used = self.usage.record(self.db_name, session, tokens)
        if self.usage.over_soft_limit(self.db_name, session):
            warnings.append(
                f"本轮已取回约 {used} token，超过软阈值 "
                f"{self.usage.soft_limit_tokens}——仅提示，不影响本次返回。"
            )

        return SessionReadResult(
            ok=True,
            session_id=str(row.get("session_id") or session_id),
            window_id=str(row.get("window_id") or ""),
            abstract_text=abstract,
            overview=overview,
            detail=detail,
            slice_count=int(row.get("coverage_total") or 0),
            model_used=row.get("model_used"),
            updated_at=str(row.get("updated_at") or ""),
            tokens=tokens,
            tokens_this_round=used,
            warnings=warnings,
        )

    # ------------------------------------------------------------------

    def read_neighbors(self, uid: str, before: int = 1, after: int = 1) -> dict[str, Any]:
        """返回相邻切片（只给摘要，让模型自己决定要不要展开某一条）。"""
        chunk = self.repo.get_chunk(uid)
        if chunk is None:
            return {"ok": False, "warnings": [f"切片不存在或已删除：{uid}"], "neighbors": []}

        items = self.repo.neighbours(chunk, before=before, after=after)
        neighbours = [
            {
                "uid": item["chunk_uid"],
                "title": item["title"] or "",
                "abstract": item["summary_text"],
                "msg_range": [item["msg_id_start"], item["msg_id_end"]],
                "current": item["chunk_uid"] == uid,
            }
            for item in items
        ]
        return {
            "ok": True,
            "uid": uid,
            "count": len(neighbours),
            "neighbors": neighbours,
            "warnings": [] if neighbours else ["没有找到相邻切片。"],
        }


__all__ = [
    "DETAIL_FULL",
    "DETAIL_MINIMAL",
    "DETAIL_STANDARD",
    "MODE_FULL",
    "MODE_QUOTE",
    "MODE_WINDOW",
    "MINIMAL_TOKEN_CAP",
    "SESSION_DETAIL_ABSTRACT",
    "SESSION_DETAIL_OVERVIEW",
    "OriginalReader",
    "ReadResult",
    "ReadUsageTracker",
    "SessionReadResult",
    "truncate_to_tokens",
]
