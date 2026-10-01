"""运行时设置覆盖。

分层：`.env` 是**启动默认值**，本模块管理的是**运行时可改的覆盖值**，
持久化到 `data/runtime_settings.json`。这样设置页的几个开关不用改 .env
也不用重启服务——而 .env 里的密钥类配置**刻意不允许从界面修改**
（没有鉴权的前提下，让 UI 能读改 API Key 不是个好主意）。

白名单机制：只有 `RUNTIME_TUNABLE` 里的字段可以覆盖。改不动的字段
（端口、模型名、维度、密钥）需要改 .env 并重启，前端会据此显示为只读。

跨进程：界面进程写这个文件，MCP 进程读——后者按文件指纹热重载
（见 `Service._maybe_hot_reload`），所以写入必须原子（临时文件 +
`os.replace`），否则读的一方会撞上半截 JSON。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import fields
from pathlib import Path
from typing import Any

from duramem.config import Settings

OVERRIDES_FILENAME = "runtime_settings.json"

# 可以从界面修改的字段。其余字段（密钥、模型名、端口、向量维度）需改 .env 并重启。
RUNTIME_TUNABLE: frozenset[str] = frozenset(
    {
        # 检索
        "vector_top_k",
        "lexical_top_k",
        "fusion_pool",
        "rrf_k",
        "rerank_top_k",
        "rerank_enabled",
        "vector_max_distance",
        "vector_weak_distance",
        # 图扩散（§16）
        "expand_enabled",
        "expand_top_k",
        "expand_always",
        # 总结
        "auto_summary_enabled",
        "summary_interval",
        "summary_min_messages",
        "summary_max_chunks",
        # 回查
        "overview_access_enabled",
        "original_access_enabled",
        "read_soft_limit_tokens",
        "read_budget_idle_reset",
        "default_window_expand",
        # 摘要
        "summary_target_tokens",
        "summary_soft_limit_tokens",
        # 网关
        "gateway_inject_memory",
    }
)

# 改这些需要重建分词器或重开监听，界面改了也不生效——明确列为重启项而不是静默忽略。
# 注意：模型名/端点/密钥**不在这里**了，它们改由 `provider_config` 管（界面可改、
# 热生效、改了会标记已有向量作废），所以不再需要重启。
NEEDS_RESTART: frozenset[str] = frozenset(
    {
        "tokenizer_extra_words",
        "tokenizer_user_dict",
        "tokenizer_stopwords_enabled",
        "data_dir",
        "backend_port",
        "frontend_port",
        "default_db",
        "sqlite_wal",
        "gateway_enabled",
        "gateway_upstream_base_url",
        "gateway_upstream_api_key",
    }
)


def settings_schema() -> list[dict[str, Any]]:
    """给前端用的字段清单：名称、类型、当前值、是否可改。"""
    return [
        {
            "name": "vector_top_k",
            "type": "int",
            "label": "向量召回条数",
            "group": "检索",
            "help": "KNN 取多少条候选进融合池",
        },
        {
            "name": "lexical_top_k",
            "type": "int",
            "label": "词法召回条数",
            "group": "检索",
            "help": "FTS5 取多少条候选进融合池",
        },
        {
            "name": "fusion_pool",
            "type": "int",
            "label": "融合池大小",
            "group": "检索",
            "help": "RRF 融合后留多少条给重排",
        },
        {
            "name": "rrf_k",
            "type": "int",
            "label": "RRF k 值",
            "group": "检索",
            "help": "越小则排名差异影响越大",
        },
        {
            "name": "rerank_enabled",
            "type": "bool",
            "label": "启用重排",
            "group": "检索",
            "help": "默认关闭：两次串行 API 往返约 300-800ms",
        },
        {
            "name": "rerank_top_k",
            "type": "int",
            "label": "重排后条数",
            "group": "检索",
            "help": "最终返回给模型的条数",
        },
        {
            "name": "vector_max_distance",
            "type": "float",
            "label": "向量距离下限",
            "group": "检索",
            "help": "余弦距离（0=同向，1=正交）。超过则剔除，刻意宽松",
        },
        {
            "name": "vector_weak_distance",
            "type": "float",
            "label": "弱相关告警线",
            "group": "检索",
            "help": "超过则只告警不剔除，让模型知道可能没有相关记忆",
        },
        {
            "name": "expand_enabled",
            "type": "bool",
            "label": "启用图扩散",
            "group": "检索",
            "help": "从直接命中沿链接图做 PPR 扩散，把多跳相关的切片增补进返回（增补不替换）",
        },
        {
            "name": "expand_top_k",
            "type": "int",
            "label": "扩散增补条数",
            "group": "检索",
            "help": "最多增补几条扩散结果，刻意取小",
        },
        {
            "name": "expand_always",
            "type": "bool",
            "label": "每次都扩散",
            "group": "检索",
            "help": "跳过触发信号常开扩散。评测/调试用，日常别开",
        },
        {
            "name": "auto_summary_enabled",
            "type": "bool",
            "label": "固定楼层自动总结",
            "group": "总结",
            "help": "默认关闭",
        },
        {
            "name": "summary_interval",
            "type": "int",
            "label": "自动总结间隔（条）",
            "group": "总结",
            "help": "累计多少条新消息触发一次",
        },
        {
            "name": "summary_min_messages",
            "type": "int",
            "label": "最少消息条数",
            "group": "总结",
            "help": "不足则拒绝总结",
        },
        {
            "name": "summary_max_chunks",
            "type": "int",
            "label": "单次最多切片数",
            "group": "总结",
            "help": "宁少而准，不要多而碎",
        },
        {
            "name": "overview_access_enabled",
            "type": "bool",
            "label": "开放会话概览",
            "group": "记忆深度",
            "help": "开启后模型可取会话级概览（含导航，指向具体切片），用于定位",
        },
        {
            "name": "original_access_enabled",
            "type": "bool",
            "label": "开放原文回查",
            "group": "记忆深度",
            "help": "关闭后模型只能看到摘要与概览，read_original 会被拒绝",
        },
        {
            "name": "read_soft_limit_tokens",
            "type": "int",
            "label": "每轮取回提示阈值（token）",
            "group": "记忆深度",
            "help": "0 = 不设限。设成正值只在超过时给一句提示，不会拒绝任何回查",
        },
        {
            "name": "read_budget_idle_reset",
            "type": "int",
            "label": "取回计数重置（秒）",
            "group": "记忆深度",
            "help": "空闲超过这个时间算新一轮，累计取回量从零算起",
        },
        {
            "name": "default_window_expand",
            "type": "int",
            "label": "window 模式默认扩展条数",
            "group": "记忆深度",
            "help": "mode=window 未指定时前后各扩展几条消息",
        },
        {
            "name": "summary_target_tokens",
            "type": "int",
            "label": "摘要目标长度（token）",
            "group": "摘要",
            "help": "按 token 而非字符：中英混排下同字符数差三倍",
        },
        {
            "name": "summary_soft_limit_tokens",
            "type": "int",
            "label": "摘要软上限（token）",
            "group": "摘要",
            "help": "超出会被标记，调试面板统计触顶比例",
        },
        {
            "name": "gateway_inject_memory",
            "type": "bool",
            "label": "网关注入记忆",
            "group": "记忆网关",
            "help": "每轮检索记忆并拼进 system prompt",
        },
    ]


class RuntimeSettings:
    """加载、应用、持久化运行时覆盖值。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.data_dir) / OVERRIDES_FILENAME
        self._lock = threading.RLock()
        self._overrides: dict[str, Any] = {}

    # ------------------------------------------------------------------

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
                key: value for key, value in raw.items() if key in RUNTIME_TUNABLE
            }
            return dict(self._overrides)

    def apply(self) -> None:
        """把覆盖值写到 Settings 对象上。必须在 Service 使用前调用。"""
        with self._lock:
            valid = {f.name for f in fields(Settings)}
            for key, value in self._overrides.items():
                if key in valid and key in RUNTIME_TUNABLE:
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

    # ------------------------------------------------------------------

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """更新覆盖值。返回 {applied, rejected}。"""
        applied: dict[str, Any] = {}
        rejected: dict[str, str] = {}

        for key, value in (patch or {}).items():
            if key in NEEDS_RESTART:
                rejected[key] = "该配置需修改 .env 并重启服务"
                continue
            if key not in RUNTIME_TUNABLE:
                rejected[key] = "不支持的配置项"
                continue
            current = getattr(self.settings, key, None)
            coerced, error = coerce_value(value, current)
            if error:
                rejected[key] = error
                continue
            setattr(self.settings, key, coerced)
            self._overrides[key] = coerced
            applied[key] = coerced

        if applied:
            self.save()
        return {"applied": applied, "rejected": rejected}

    def reset(
        self, keys: list[str] | None = None, settings_from_env: Settings | None = None
    ) -> dict[str, Any]:
        """清除覆盖值，回落到 .env 的默认。"""
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

    # ------------------------------------------------------------------

    def description(self) -> dict[str, Any]:
        """给前端的完整视图：可改项当前值 + 只读项 + 生效来源。"""
        values: dict[str, Any] = {}
        names: set[str] = set()
        for item in settings_schema():
            name = item["name"]
            names.add(name)
            entry = dict(item)
            entry["value"] = getattr(self.settings, name, None)
            entry["overridden"] = name in self._overrides
            values[name] = entry

        readonly: dict[str, Any] = {}
        for field in fields(Settings):
            if field.name in names:
                continue
            if "key" in field.name:
                readonly[field.name] = "***" if getattr(self.settings, field.name) else ""
            else:
                value = getattr(self.settings, field.name)
                readonly[field.name] = str(value) if isinstance(value, Path) else value

        return {
            "tunable": list(values.values()),
            "readonly": readonly,
            "needs_restart": sorted(NEEDS_RESTART),
            "overrides": dict(self._overrides),
            "overrides_file": str(self.path),
        }


def coerce_value(value: Any, current: Any) -> tuple[Any, str | None]:
    """把前端来的值转成正确类型，并做基本范围校验。

    `provider_config` 也用它，好让两处对"什么算合法输入"的判断不会各说各话。
    """
    try:
        if isinstance(current, bool):
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"true", "1", "on", "yes"}:
                    return True, None
                if lowered in {"false", "0", "off", "no"}:
                    return False, None
                return None, f"无法解析为布尔值：{value}"
            return bool(value), None

        if isinstance(current, int) and not isinstance(current, bool):
            ivalue = int(value)
            if ivalue < 0:
                return None, "不能为负数"
            return ivalue, None

        if isinstance(current, float):
            fvalue = float(value)
            if fvalue < 0:
                return None, "不能为负数"
            return fvalue, None

        return value, None
    except (TypeError, ValueError):
        return None, f"类型不符，期望 {type(current).__name__}"


__all__ = [
    "NEEDS_RESTART",
    "OVERRIDES_FILENAME",
    "RUNTIME_TUNABLE",
    "RuntimeSettings",
    "coerce_value",
    "settings_schema",
]
