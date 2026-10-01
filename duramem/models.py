"""共享数据模型与规范化渲染约定。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    """一条原始消息。采集适配器的输出单位。"""

    window_id: str
    session_id: str
    seq: int
    role: str
    content: str
    speaker: str | None = None
    ts: str | None = None
    source_msg_id: str | None = None


def message_label(msg: Message | dict[str, Any]) -> str:
    """渲染时使用的说话人标签。"""
    if isinstance(msg, dict):
        return str(msg.get("speaker") or msg.get("role") or "unknown")
    return str(msg.speaker or msg.role or "unknown")


def render_message_line(msg: Message | dict[str, Any]) -> str:
    """单条消息的规范化渲染：`说话人: 内容`。

    为什么带前缀：聊天场景下"谁说的"是原文语义的一部分，
    主 LLM 回捞原文时必须能区分是用户说的还是助手说的。
    """
    if isinstance(msg, dict):
        content = str(msg.get("content") or "")
    else:
        content = msg.content
    return f"{message_label(msg)}: {content}"


def render_messages_with_offsets(
    msgs: list[Any],
) -> tuple[str, list[tuple[int, int, int]]]:
    """渲染消息并把每行的字符区间一并算出来。

    返回 `(text, [(msg_id, start, end), ...])`。文本与区间在同一循环里产出，
    保证二者永不漂移——`original_text` 与 `original_char_start/end` 的可校验性依赖这一点。
    """
    lines: list[str] = []
    offsets: list[tuple[int, int, int]] = []
    cursor = 0
    for msg in msgs:
        line = render_message_line(msg)
        msg_id = msg["id"] if isinstance(msg, dict) else getattr(msg, "seq", None)
        if isinstance(msg, dict) and "id" in msg:
            msg_id = int(msg["id"])
        offsets.append((int(msg_id) if msg_id is not None else -1, cursor, cursor + len(line)))
        lines.append(line)
        cursor += len(line) + 1  # 含换行符
    return "\n".join(lines), offsets


def render_messages(msgs: list[Any]) -> str:
    """把一串消息渲染成规范化的 L2 原文。

    这个函数是 `original_text` 与 `original_char_start/end` 的唯一约定来源——
    偏移量都相对本函数的输出计算，因此可重复推导、可校验。
    """
    return render_messages_with_offsets(msgs)[0]


# 参与"原文"渲染的角色。
#
# 导入器会把宿主注入的提醒与助手的过程叙述标成 `system` 角色——它们留在库里
# （不丢数据），但不该出现在 L2 原文里：用户想回查的"原文"是**真实对话**，
# 不是宿主的管道消息。混进来还有实际代价：一个只含 2 条对话消息的切片，
# 若区间里夹着 8 条系统提醒，L2 会膨胀到上万字符，模型回查时直接被预算拒掉。
CONVERSATIONAL_ROLES = ("user", "assistant")


def is_conversational(role: Any) -> bool:
    return str(role or "").lower() in CONVERSATIONAL_ROLES


@dataclass
class ChunkDraft:
    """摘要模型产出的待写入切片（条目层）。

    注意这里没有 L1：L1 是**会话**级的概览层，落在 `session_layers` 表，
    不随切片走。切片只有 L0（检索匹配入口）与 L2（原文）。

    `original_text` 与 `original_char_start/end` 都不是模型给的——它们由服务从 `msg_id`
    区间自己渲染推导（见 `summarizer._validate`），模型给的会被丢弃并告警。
    """

    summary_text: str
    original_text: str = ""
    title: str = ""
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    msg_id_start: int | None = None
    msg_id_end: int | None = None
    original_char_start: int | None = None
    original_char_end: int | None = None
    source_window: str | None = None
    source_session: str | None = None
    weight: float = 1.0
    title_suggested: str | None = None


@dataclass
class SearchHit:
    """一条检索结果。字段刻意对齐 MCP 返回，便于模型判断是否深入。"""

    uid: str
    chunk_id: int
    title: str
    summary_text: str
    score: float = 0.0
    tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    weight: float = 1.0
    hit_count: int = 0
    has_original: bool = False
    # 该切片所属会话。模型拿到它就能去取会话概览（L1）——这是"容器索引"的入口。
    # 只给标识不给概览正文：概览有 4000 token，随每个命中返回会在 top_k=5 时
    # 灌进约 2 万 token，而照 OpenViking 它本该是按需取的。
    session_id: str | None = None
    msg_id_start: int | None = None
    msg_id_end: int | None = None
    # 调试信息
    rrf_score: float = 0.0
    vec_rank: int | None = None
    vec_distance: float | None = None
    lex_rank: int | None = None
    lex_score: float | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None
    db: str | None = None
    suggestion: str | None = None
    # 图扩散来源（§16）：direct = 直接命中（默认，绝不缺省为 expand）。
    # expand = 该切片由 PPR 扩散带出——直接命中保证原样在前，扩散只做增补。
    origin: str = "direct"
    expand_score: float | None = None   # 归一化 PPR 分（最高者 1.0）
    expand_via: str | None = None       # 主导种子的 chunk_uid
    expand_relation: str | None = None  # 路径第一跳的关系（adjacent / tag / keyword）
    expand_hops: int | None = None      # 距种子的跳数

    def to_mcp_dict(self) -> dict[str, Any]:
        """给主 LLM 看的形态。只暴露决策所需字段，不带内部排名。"""
        payload: dict[str, Any] = {
            "uid": self.uid,
            "title": self.title,
            "summary": self.summary_text,
            "score": round(self.score, 4),
            "hit_count": self.hit_count,
            "has_original": self.has_original,
        }
        if self.origin == "expand":
            payload["origin"] = "expand"
            payload["expand_score"] = round(self.expand_score or 0.0, 4)
            if self.expand_via:
                payload["expand_via"] = self.expand_via
            if self.expand_relation:
                payload["expand_relation"] = self.expand_relation
            if self.expand_hops is not None:
                payload["expand_hops"] = self.expand_hops
        if self.tags:
            payload["tags"] = self.tags
        if self.db:
            payload["db"] = self.db
        if self.session_id:
            # 只有标识，没有概览正文——模型据此决定要不要取会话概览（L1）
            payload["session_id"] = self.session_id
        if self.msg_id_start is not None and self.msg_id_end is not None:
            payload["msg_range"] = [self.msg_id_start, self.msg_id_end]
        if self.suggestion:
            payload["_suggestion"] = self.suggestion
        return payload

    def to_debug_dict(self) -> dict[str, Any]:
        """给检索调试面板看的形态：三路原始排名全暴露。"""
        return {
            "uid": self.uid,
            "title": self.title,
            "summary": self.summary_text,
            "rrf_score": round(self.rrf_score, 6),
            "float_score": round(self.score, 6),
            "vec_rank": self.vec_rank,
            "vec_distance": None if self.vec_distance is None else round(self.vec_distance, 6),
            "lex_rank": self.lex_rank,
            "lex_score": None if self.lex_score is None else round(self.lex_score, 6),
            "rerank_rank": self.rerank_rank,
            "rerank_score": self.rerank_score,
            "weight": self.weight,
            "hit_count": self.hit_count,
            "arm": _arm_label(self.vec_rank, self.lex_rank),
        }


def _arm_label(vec_rank: int | None, lex_rank: int | None) -> str:
    if vec_rank is not None and lex_rank is not None:
        return "both"
    if vec_rank is not None:
        return "vector_only"
    if lex_rank is not None:
        return "lexical_only"
    return "none"
