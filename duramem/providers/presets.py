"""模型提供方预设。

这不是"支持列表"——任何 OpenAI 兼容端点都能用，填自定义值即可。
预设只是把几个常用组合的 `base_url` / 模型名 / 维度 / 批量填好，
省掉"去翻文档抄模型名"那一步。

值都会在保存前被「测试连接」当场验证，所以填错不会静默生效。
批量大小按各家文档的上限取，宁可保守：百炼的 text-embedding-v3/v4
一批最多 10 条，v1/v2 是 25 条，超了直接 400。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Preset:
    id: str
    group: str  # embedding | rerank | summary
    label: str
    vendor: str
    values: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    needs_key: bool = True


SILICONFLOW = "https://api.siliconflow.cn/v1"
DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
OLLAMA = "http://127.0.0.1:11434/v1"

PRESETS: tuple[Preset, ...] = (
    # ------------------------------------------------------------ 嵌入
    Preset(
        id="siliconflow-bge-m3",
        group="embedding",
        label="BAAI/bge-m3",
        vendor="硅基流动",
        values={
            "embedding_base_url": SILICONFLOW,
            "embedding_model": "BAAI/bge-m3",
            "embedding_dim": 1024,
            "embed_batch_size": 32,
            "embedding_send_dimensions": False,
        },
        note="默认推荐：中文强、1024 维、便宜，与默认的距离阈值一起调过",
    ),
    Preset(
        id="dashscope-v4",
        group="embedding",
        label="text-embedding-v4",
        vendor="阿里云百炼",
        values={
            "embedding_base_url": DASHSCOPE,
            "embedding_model": "text-embedding-v4",
            "embedding_dim": 1024,
            "embed_batch_size": 10,
            "embedding_send_dimensions": True,
        },
        note="可选 2048/1536/1024/768/512/256/128/64 维；单请求最多 10 条",
    ),
    Preset(
        id="dashscope-v3",
        group="embedding",
        label="text-embedding-v3",
        vendor="阿里云百炼",
        values={
            "embedding_base_url": DASHSCOPE,
            "embedding_model": "text-embedding-v3",
            "embedding_dim": 1024,
            "embed_batch_size": 10,
            "embedding_send_dimensions": True,
        },
        note="v4 的前代，单请求最多 10 条",
    ),
    Preset(
        id="ollama-bge-m3",
        group="embedding",
        label="bge-m3（本地）",
        vendor="Ollama",
        values={
            "embedding_base_url": OLLAMA,
            "embedding_model": "bge-m3",
            "embedding_dim": 1024,
            "embed_batch_size": 8,
            "embedding_send_dimensions": False,
        },
        note="全本地、不需要 Key；先 `ollama pull bge-m3`。API Key 随便填一个非空值即可",
        needs_key=False,
    ),
    Preset(
        id="openai-3-small",
        group="embedding",
        label="text-embedding-3-small",
        vendor="OpenAI",
        values={
            "embedding_base_url": "https://api.openai.com/v1",
            "embedding_model": "text-embedding-3-small",
            "embedding_dim": 1536,
            "embed_batch_size": 32,
            "embedding_send_dimensions": True,
        },
        note="1536 维；改维度后必须重建向量",
    ),
    # ------------------------------------------------------------ 重排
    Preset(
        id="siliconflow-reranker",
        group="rerank",
        label="BAAI/bge-reranker-v2-m3",
        vendor="硅基流动",
        values={
            "rerank_base_url": SILICONFLOW,
            "rerank_model": "BAAI/bge-reranker-v2-m3",
        },
        note="与默认嵌入搭配的多语言重排模型",
    ),
    Preset(
        id="jina-reranker",
        group="rerank",
        label="jina-reranker-v2-base-multilingual",
        vendor="Jina AI",
        values={
            "rerank_base_url": "https://api.jina.ai/v1",
            "rerank_model": "jina-reranker-v2-base-multilingual",
        },
    ),
    # ------------------------------------------------------------ 摘要
    Preset(
        id="deepseek-chat",
        group="summary",
        label="deepseek-chat",
        vendor="DeepSeek",
        values={
            "summary_base_url": "https://api.deepseek.com/v1",
            "summary_model": "deepseek-chat",
        },
        note="把对话压成切片要它稳定输出 JSON，这个够用且便宜",
    ),
    Preset(
        id="dashscope-qwen-plus",
        group="summary",
        label="qwen-plus",
        vendor="阿里云百炼",
        values={
            "summary_base_url": DASHSCOPE,
            "summary_model": "qwen-plus",
        },
    ),
    Preset(
        id="siliconflow-qwen",
        group="summary",
        label="Qwen/Qwen2.5-7B-Instruct",
        vendor="硅基流动",
        values={
            "summary_base_url": SILICONFLOW,
            "summary_model": "Qwen/Qwen2.5-7B-Instruct",
        },
    ),
    Preset(
        id="ollama-qwen",
        group="summary",
        label="qwen2.5:7b（本地）",
        vendor="Ollama",
        values={
            "summary_base_url": OLLAMA,
            "summary_model": "qwen2.5:7b",
        },
        note="全本地；先 `ollama pull qwen2.5:7b`。API Key 随便填一个非空值即可",
        needs_key=False,
    ),
)

PRESETS_BY_ID: dict[str, Preset] = {preset.id: preset for preset in PRESETS}

GROUPS = ("embedding", "rerank", "summary")


def presets_for(group: str) -> list[dict[str, Any]]:
    """给前端的预设清单（只含选定 group）。"""
    return [
        {
            "id": preset.id,
            "label": preset.label,
            "vendor": preset.vendor,
            "values": dict(preset.values),
            "note": preset.note,
            "needs_key": preset.needs_key,
        }
        for preset in PRESETS
        if preset.group == group
    ]


__all__ = [
    "DASHSCOPE",
    "GROUPS",
    "OLLAMA",
    "PRESETS",
    "PRESETS_BY_ID",
    "SILICONFLOW",
    "Preset",
    "presets_for",
]
