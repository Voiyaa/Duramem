"""采集适配器：把宿主的对话原文喂进记忆库。

为什么需要这一层：**MCP 协议本身没有"读取宿主对话历史"的能力**——它只有
tools / resources / prompts / sampling。所以"挂载到聊天窗口"实际是两个独立的集成面：

1. **MCP**：给模型工具（`dm_search` / `dm_read_original`），模型有主动权
2. **采集**：把原文灌进 `original_text`，没有它切片就没有可回查的原文

两者都要，缺一不可。本模块是第 2 个面的可插拔实现。

三条已知路线（按接入成本排序）：

| 路线 | 适用 | 代价 |
|---|---|---|
| OpenAI 兼容网关 | 任何能改 base_url 的客户端 | 无需宿主配合，但模型被动（无工具调用权） |
| Claude Code hook | Claude Code | 读 `PreCompact` 给的 `transcript_path` |
| 窗口内扩展 | SillyTavern 等 | 需要为每个宿主写扩展，但能拿到内存里的完整 chat |
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from duramem.models import Message


class CollectorAdapter(Protocol):
    """采集适配器接口。

    实现者只需要回答两个问题：这个窗口有哪些会话、某个会话里有什么消息。
    写库、去重、游标推进都由 Service 负责。
    """

    name: str

    def register(self, window_id: str, session_id: str) -> None:
        """窗口启动时登记，用于来源标注与总结游标。"""
        ...

    def fetch_messages(
        self, window_id: str, session_id: str, since_seq: int | None = None
    ) -> list[Message]:
        """取某个会话的消息。`since_seq` 用于增量拉取。"""
        ...


def normalize_content(content: Any) -> str:
    """把 OpenAI 风格的 content 归一成纯文本。

    多模态消息里 content 是数组（`[{"type":"text","text":...}, {"type":"image_url",...}]`），
    这里只取文本部分——图片本身不该进记忆切片（要存也可以，但那是另一个设计）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
                elif "text" in item and isinstance(item["text"], str):
                    parts.append(item["text"])
        return "\n".join(part for part in parts if part)
    return str(content)


def openai_messages_to_messages(
    payload: Sequence[dict[str, Any]],
    window_id: str,
    session_id: str,
    seq_lookup=None,
) -> list[Message]:
    """把 OpenAI 格式的 messages 转成内部 Message。

    seq 的分配策略很关键：**不能简单用数组下标**。聊天客户端为了控制上下文
    会从头裁掉旧消息，下标一旦前移，同一 seq 位置上的内容就变了，
    按 (window, session, seq) 去重会互相覆盖。

    所以优先按内容哈希反查已有消息的 seq（内容没变就复用原位置），
    查不到才追加到末尾。这样裁剪、重排、重发都不会破坏已有记录。
    """
    out: list[Message] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        content = normalize_content(item.get("content"))
        if not content.strip():
            # 纯 function call / 纯图片的消息没有可记忆的文本
            continue
        role = str(item.get("role") or "user")
        speaker = item.get("name") or item.get("speaker")
        seq = None
        if seq_lookup is not None:
            seq = seq_lookup(window_id, session_id, content, index)
        out.append(
            Message(
                window_id=window_id,
                session_id=session_id,
                seq=int(seq if seq is not None else index),
                role=role,
                content=content,
                speaker=str(speaker) if speaker else None,
                source_msg_id=str(item.get("id")) if item.get("id") else None,
            )
        )
    return out


__all__ = [
    "CollectorAdapter",
    "normalize_content",
    "openai_messages_to_messages",
]
