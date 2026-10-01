"""模型提供方配置：界面可改、热生效、密钥只写不可读。

分层与 `runtime_settings.py` 平行，但管的是不同的东西：

- `runtime_settings.py` → 检索/总结/回查的行为参数（数值开关）
- 本模块 → 模型提供方本身（端点、模型名、维度、密钥）

**为什么要分成两个文件**：.env 是启动默认值，模型配置需要能随时改
（换嵌入模型是开发期最频繁的操作之一），而且改完必须能立刻生效、
并让界面知道"已有向量作废了"。把它塞进 runtime_settings 的白名单会
让那份"只允许改数值"的约束失效。

**密钥的处理**：写入可以，读回只给掩码。理由是没有鉴权的前提下不该让
接口能读回明文密钥——但写是低风险的，所以不做成"只能改 .env"。
服务默认只监听 127.0.0.1，且没有配 CORS 中间件，
浏览器跨源预检过不去，别的页面没法从外部触发写入。

**跨进程**：界面进程写 `provider_config.json`，MCP 进程按文件指纹热重载
（见 `Service._maybe_hot_reload`），写入必须原子（临时文件 + `os.replace`）。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from duramem.config import Settings
from duramem.providers.embedding import describe_embedding_provider
from duramem.runtime_settings import coerce_value

PROVIDER_CONF_FILENAME = "provider_config.json"

GROUP_LABELS = {
    "embedding": "嵌入模型",
    "rerank": "重排模型",
    "summary": "摘要模型",
}

GROUP_HELP = {
    "embedding": "决定语义检索。改动会让库里已有向量失效，需要在每张库上重建。",
    "rerank": "可选，默认关闭。开启后每次检索多一次 API 往返（约 300-800ms）。",
    "summary": "把对话压成切片的模型，必须能稳定输出 JSON。不填则用离线抽取式摘要。",
}


@dataclass(frozen=True)
class ProviderField:
    name: str
    group: str
    label: str
    type: str  # text | secret | int | bool
    help: str = ""
    placeholder: str = ""

    @property
    def secret(self) -> bool:
        return self.type == "secret"


PROVIDER_FIELDS: tuple[ProviderField, ...] = (
    # ------------------------------------------------------------ 嵌入
    ProviderField(
        "embedding_base_url",
        "embedding",
        "接口地址",
        "text",
        placeholder="https://api.siliconflow.cn/v1",
        help="OpenAI 兼容端点，末尾不需要 /embeddings",
    ),
    ProviderField(
        "embedding_model",
        "embedding",
        "模型名",
        "text",
        placeholder="BAAI/bge-m3",
        help="原样透传给接口的 model 字段",
    ),
    ProviderField(
        "embedding_api_key",
        "embedding",
        "API Key",
        "secret",
        help="保存后不再回显，只显示掩码。留空表示保持原值不变",
    ),
    ProviderField(
        "embedding_dim",
        "embedding",
        "向量维度",
        "int",
        help="必须与模型实际输出一致，否则写入会直接报错（这是故意的）",
    ),
    ProviderField(
        "embed_batch_size",
        "embedding",
        "每批条数",
        "int",
        help="百炼 text-embedding-v3/v4 上限 10 条，超了接口返回 400",
    ),
    ProviderField(
        "embedding_send_dimensions",
        "embedding",
        "请求里带 dimensions",
        "bool",
        help="仅百炼 v3/v4、OpenAI v3 等支持按维度裁剪的模型需要打开",
    ),
    ProviderField(
        "embedding_fake",
        "embedding",
        "强制离线哈希嵌入",
        "bool",
        help="打开后不调接口，用确定性哈希向量。仅供开发调试，语义检索会失效",
    ),
    # ------------------------------------------------------------ 重排
    ProviderField(
        "rerank_enabled",
        "rerank",
        "启用重排",
        "bool",
        help="关掉时按融合顺序返回，链路完整可用",
    ),
    ProviderField(
        "rerank_base_url",
        "rerank",
        "接口地址",
        "text",
        placeholder="https://api.siliconflow.cn/v1",
        help="需要支持 /rerank（硅基流动、Jina、Cohere 风格）",
    ),
    ProviderField(
        "rerank_model",
        "rerank",
        "模型名",
        "text",
        placeholder="BAAI/bge-reranker-v2-m3",
    ),
    ProviderField(
        "rerank_api_key",
        "rerank",
        "API Key",
        "secret",
        help="留空表示保持原值不变",
    ),
    # ------------------------------------------------------------ 摘要
    ProviderField(
        "summary_base_url",
        "summary",
        "接口地址",
        "text",
        placeholder="https://api.deepseek.com/v1",
        help="留空则用离线抽取式摘要（不调模型）",
    ),
    ProviderField(
        "summary_model",
        "summary",
        "模型名",
        "text",
        placeholder="deepseek-chat",
    ),
    ProviderField(
        "summary_api_key",
        "summary",
        "API Key",
        "secret",
        help="留空表示保持原值不变",
    ),
)

PROVIDER_FIELD_NAMES: frozenset[str] = frozenset(f.name for f in PROVIDER_FIELDS)
PROVIDER_BY_NAME: dict[str, ProviderField] = {f.name: f for f in PROVIDER_FIELDS}
SECRET_FIELDS: frozenset[str] = frozenset(f.name for f in PROVIDER_FIELDS if f.secret)

# 换了嵌入模型/维度就必须重建向量的字段。改这些之外的字段不动向量。
VECTOR_IDENTITY_FIELDS: tuple[str, ...] = (
    "embedding_model",
    "embedding_dim",
    "embedding_api_key",
    "embedding_base_url",
    "embedding_fake",
    "embedding_send_dimensions",
)


def mask_secret(value: str) -> str:
    """密钥掩码。够用来辨认"是不是同一把钥匙"，不够用来还原。"""
    if not value:
        return ""
    text = str(value)
    if len(text) <= 10:
        return "***"
    return f"{text[:4]}…{text[-4:]}"


def _with_auto_offline_clear(
    patch: dict[str, Any], current: Settings
) -> tuple[dict[str, Any], bool]:
    """填了 Key 就把"自动打开的强制离线"关掉。

    没填 Key 时 `config.py` 会把 `embedding_fake` 自动翻成 True。于是
    "在界面上填一把 Key"这个动作会**什么都不发生**——强制离线还开着。
    填 Key 就是想用真实模型，所以顺手关掉它；同一次提交里显式要求开着的除外。

    `update()` 与 `draft_settings()` 共用这一条：连通性测试必须测出
    "保存后会怎样"，两边规则不一致的话，测试会说一个结果、保存得到另一个。
    """
    patch = dict(patch or {})
    raw_key = patch.get("embedding_api_key")
    if (
        isinstance(raw_key, str)
        and raw_key.strip()
        and "embedding_fake" not in patch
        and getattr(current, "embedding_fake", False)
    ):
        patch["embedding_fake"] = False
        return patch, True
    return patch, False


class ProviderConfig:
    """模型配置的读写与热生效。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.data_dir) / PROVIDER_CONF_FILENAME
        self._lock = threading.RLock()
        self._overrides: dict[str, Any] = {}

    # ------------------------------------------------------------------ 持久化

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                self._overrides = {}
                return {}
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # 保留已有覆盖值而不是清空：热重载场景下文件读失败意味着
                # 下一次工具调用还会再试，打回空集会让本进程悄悄丢掉全部覆盖
                return dict(self._overrides)
            self._overrides = {
                key: value
                for key, value in raw.items()
                if key in PROVIDER_FIELD_NAMES and not _is_secret_placeholder(value)
            }
            return dict(self._overrides)

    def apply(self) -> None:
        """把覆盖值写到 Settings 对象上。必须在 Service 用它之前调用。"""
        with self._lock:
            for key, value in self._overrides.items():
                if key in PROVIDER_FIELD_NAMES:
                    setattr(self.settings, key, value)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 原子写：MCP 进程会按 mtime 热重载这个文件，不能让它读到半截 JSON
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._overrides, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)

    # ------------------------------------------------------------------ 读

    def effective_embedding(self) -> dict[str, Any]:
        """当前实际会用哪个嵌入提供方。判定与 build_embedding_provider 同源。"""
        provider, offline = describe_embedding_provider(
            api_key=self.settings.embedding_api_key,
            model=self.settings.embedding_model,
            dim=self.settings.embedding_dim,
            force_offline=self.settings.embedding_fake,
        )
        return {
            "provider": provider,
            "model": self.settings.embedding_model,
            "dim": self.settings.embedding_dim,
            "offline": offline,
            "batch_size": self.settings.embed_batch_size,
            "base_url": self.settings.embedding_base_url,
            "send_dimensions": self.settings.embedding_send_dimensions,
            "has_key": bool(self.settings.embedding_api_key),
            "key_masked": mask_secret(self.settings.embedding_api_key),
        }

    def description(self) -> dict[str, Any]:
        """给前端的完整视图：字段 + 当前值（密钥掩码）+ 分组说明。"""
        fields: list[dict[str, Any]] = []
        for field in PROVIDER_FIELDS:
            value = getattr(self.settings, field.name, None)
            raw = "" if value is None else value
            fields.append(
                {
                    "name": field.name,
                    "group": field.group,
                    "label": field.label,
                    "type": field.type,
                    "help": field.help,
                    "placeholder": field.placeholder,
                    "value": "" if field.secret else raw,
                    "masked": mask_secret(str(raw)) if field.secret else "",
                    "has_value": bool(raw) if field.secret else None,
                    "overridden": field.name in self._overrides,
                }
            )

        return {
            "fields": fields,
            "overrides": {
                key: (mask_secret(str(value)) if key in SECRET_FIELDS else value)
                for key, value in self._overrides.items()
            },
            "overrides_file": str(self.path),
            "groups": [
                {"id": group, "label": GROUP_LABELS[group], "help": GROUP_HELP[group]}
                for group in ("embedding", "rerank", "summary")
            ],
            "effective_embedding": self.effective_embedding(),
            "rerank": {
                "enabled": bool(self.settings.rerank_enabled),
                "model": self.settings.rerank_model,
                "base_url": self.settings.rerank_base_url,
                "has_key": bool(self.settings.rerank_api_key),
                "key_masked": mask_secret(self.settings.rerank_api_key),
                "effective": (
                    "offline-overlap"
                    if (self.settings.rerank_enabled and not self.settings.rerank_api_key)
                    else (
                        "none"
                        if not self.settings.rerank_enabled
                        else self.settings.rerank_model
                    )
                ),
            },
            "summary": {
                "model": self.settings.summary_model,
                "base_url": self.settings.summary_base_url,
                "has_key": bool(self.settings.summary_api_key),
                "key_masked": mask_secret(self.settings.summary_api_key),
                "effective": (
                    self.settings.summary_model
                    if (self.settings.summary_api_key and self.settings.summary_base_url)
                    else "offline-heuristic"
                ),
            },
        }

    # ------------------------------------------------------------------ 写

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """更新模型配置。返回 {applied, rejected}。

        密钥字段收到空串/掩码占位符时**视为"不改"**——否则界面上
        留空保存就会把已存好的 Key 抹掉，这种静默破坏最难受。
        """
        patch = dict(patch or {})

        # 见 _with_auto_offline_clear：填了 Key 就不该还被强制离线的开关挡住
        patch, auto_cleared_offline = _with_auto_offline_clear(patch, self.settings)

        applied: dict[str, Any] = {}
        rejected: dict[str, str] = {}

        with self._lock:
            for key, value in patch.items():
                field = PROVIDER_BY_NAME.get(key)
                if field is None:
                    rejected[key] = "不是可配置的模型字段"
                    continue

                if field.secret and _is_secret_placeholder(value):
                    continue

                current = getattr(self.settings, key, None)
                coerced, error = _coerce(value, current)

                if error:
                    rejected[key] = error
                    continue

                if field.type == "int" and isinstance(coerced, int):
                    coerced, error = _check_int_range(key, coerced)
                    if error:
                        rejected[key] = error
                        continue

                if field.type == "text" and isinstance(coerced, str):
                    coerced = coerced.strip()
                    if "\n" in coerced or "\r" in coerced:
                        rejected[key] = "不能包含换行"
                        continue
                    if not coerced and field.label == "模型名":
                        # 空模型名发给接口只会换来一句难懂的 400，这里先挡住
                        rejected[key] = "模型名不能为空"
                        continue

                setattr(self.settings, key, coerced)
                self._overrides[key] = coerced
                applied[key] = mask_secret(str(coerced)) if field.secret else coerced

            if applied:
                self.save()

        return {
            "applied": applied,
            "rejected": rejected,
            "auto_cleared_offline": auto_cleared_offline,
        }

    def reset(
        self, keys: list[str] | None = None, settings_from_env: Settings | None = None
    ) -> dict[str, Any]:
        """清除覆盖，回落到 .env 默认值。"""
        with self._lock:
            targets = list(keys) if keys else list(self._overrides)
            for key in targets:
                self._overrides.pop(key, None)
            self.save()

        if settings_from_env is not None:
            for key in targets:
                if hasattr(settings_from_env, key):
                    setattr(self.settings, key, getattr(settings_from_env, key))
        return {"reset": targets}

    def draft_settings(self, patch: dict[str, Any]) -> Settings:
        """在**不改动现值**的前提下，算出"如果这样填会得到什么配置"。

        连通性测试要拿草稿去试，不能拿已保存的值试——否则用户填完点测试，
        测的是旧配置，通过之后保存了照样是坏的。

        这里必须走与 `update()` 完全相同的规则（含自动关掉强制离线），
        否则测试结果与实际保存结果会不一致。
        """
        from dataclasses import fields as dataclass_fields

        clone = Settings()
        # 循环变量避开 `field`：下面那个循环里 `field` 是 ProviderField，
        # 同名复用会让 mypy 把两次含义混起来（也是真实的读代码障碍）。
        for entry in dataclass_fields(Settings):
            setattr(clone, entry.name, getattr(self.settings, entry.name))

        patch, _ = _with_auto_offline_clear(patch, self.settings)

        for key, value in patch.items():
            field = PROVIDER_BY_NAME.get(key)
            if field is None:
                continue
            if field.secret and _is_secret_placeholder(value):
                continue
            coerced, error = _coerce(value, getattr(clone, key, None))
            if error:
                raise ValueError(f"{key}：{error}")
            setattr(clone, key, coerced)
        return clone


def _check_int_range(key: str, value: int) -> tuple[int | None, str | None]:
    if key == "embedding_dim":
        if value < 8 or value > 8192:
            return None, "维度应在 8 到 8192 之间"
        return value, None
    if key == "embed_batch_size":
        if value < 1 or value > 256:
            return None, "每批条数应在 1 到 256 之间（百炼 v3/v4 上限是 10）"
        return value, None
    return value, None


def _is_secret_placeholder(value: Any) -> bool:
    """空串、纯掩码、或含掩码省略号的字符串都表示"别动这个密钥"。"""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    text = value.strip()
    return text == "" or text == "***" or "…" in text


def _coerce(value: Any, current: Any) -> tuple[Any, str | None]:
    """按当前值的类型转一下。复用 runtime_settings 的规则以免两套语义。"""
    return coerce_value(value, current)


__all__ = [
    "GROUP_HELP",
    "GROUP_LABELS",
    "PROVIDER_BY_NAME",
    "PROVIDER_CONF_FILENAME",
    "PROVIDER_FIELDS",
    "PROVIDER_FIELD_NAMES",
    "SECRET_FIELDS",
    "VECTOR_IDENTITY_FIELDS",
    "ProviderConfig",
    "ProviderField",
    "mask_secret",
]
