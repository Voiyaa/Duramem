"""配置层：从 .env / 环境变量读取，全部有可用默认值。

无 API Key 时自动降级为离线伪向量（EMBEDDING_FAKE），保证离线可开发可测试。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from dotenv import load_dotenv


def _str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value.strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _list(name: str) -> list[str]:
    raw = _str(name)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    # 服务
    backend_port: int = 8001
    frontend_port: int = 5174
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    default_db: str = "duramem"

    # Embedding
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    embedding_fake: bool = False
    embed_batch_size: int = 32
    # 请求里是否带 `dimensions` 参数。只有支持按维度裁剪的模型才打开
    # （百炼 text-embedding-v3/v4、OpenAI text-embedding-3-*）。
    # 默认关闭：给不支持它的严格端点多传字段会被 400 拒绝。
    embedding_send_dimensions: bool = False

    # 重排
    rerank_enabled: bool = False
    rerank_api_key: str = ""
    rerank_base_url: str = "https://api.siliconflow.cn/v1"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # 摘要 / 主模型
    summary_api_key: str = ""
    summary_base_url: str = ""
    summary_model: str = "deepseek-chat"
    main_model_api_key: str = ""
    main_model_base_url: str = ""
    main_model: str = ""

    # 检索
    vector_top_k: int = 20
    lexical_top_k: int = 20
    fusion_pool: int = 30
    rrf_k: int = 60
    rerank_top_k: int = 5

    # 向量相关性下限（余弦距离：0=同向，1=正交，2=反向）。
    # 刻意设得宽松——只剔除近似正交的结果。原因是"好的相似度阈值"与嵌入模型强相关，
    # 收紧它风险很大；需要更严时改这个值，但不要指望它是通用常数。
    vector_max_distance: float = 0.9
    # 超过此值只告警不剔除，让模型知道"本次结果相关性偏低，可能没有相关记忆"
    vector_weak_distance: float = 0.75
    # sqlite-vec 的向量分配块大小。默认 1024 意味着 1024 条 × 1024 维 × 4 字节
    # = 4 MB 的固定下限，与库里实际有几条向量无关。实测改成 64 不影响 KNN 延迟
    # （差异在噪声内），但把下限降到约 288 KB。多库场景下值得。
    vector_chunk_size: int = 64

    # 总结
    auto_summary_enabled: bool = False
    summary_interval: int = 30
    summary_min_messages: int = 4
    summary_max_chunks: int = 8

    # 回查
    # 两个独立开关，对应两种日常模式：
    #   都关 = 日常模式（模型只看得到 L0 摘要）
    #   都开 = 工作模式（L0 → 会话概览 L1 → 切片原文 L2，由模型逐层自己决定）
    #
    # 注意环境变量名**刻意不对称**：旧的 `L1_ACCESS_ENABLED` 意思是"开放原文
    # 回查"，而三层之后 L1 指的是会话概览。若让新的 L1 开关沿用这个名字，
    # 老配置里的 `L1_ACCESS_ENABLED=false` 会从"保护原文"悄悄变成"关掉概览"，
    # 把原文反而开放出去。所以：
    #   overview_access_enabled ← OVERVIEW_ACCESS_ENABLED（新名，无历史包袱）
    #   original_access_enabled ← ORIGINAL_ACCESS_ENABLED，回退读旧的 L1_ACCESS_ENABLED
    overview_access_enabled: bool = True
    original_access_enabled: bool = True
    # 原文取回的**软**阈值（token）。0 = 不设限，读多少给多少——照 OpenViking,
    # 它对原文读取不设上限，体量靠"定位精度"控制而不是限额。设成正数也只是
    # 在返回里加一句提示，**不会拒绝**任何回查。
    # 历史：这个键原先叫 READ_BUDGET_TOKENS 且是硬限额（默认 4000），会拒绝
    # 超额的读取并退化成摘要。那条设计已撤销：实测它把一条 7956 token 的区间
    # 拒掉、只给 500 token，丢 94%，而现代上下文完全装得下。
    read_soft_limit_tokens: int = 0
    read_budget_idle_reset: int = 120
    default_window_expand: int = 1

    # 摘要（切片层）
    summary_target_tokens: int = 180
    summary_soft_limit_tokens: int = 256

    # 会话层（容器层）。概览由模型产出，简介由概览首段机械抽取派生。
    # L1 是**地图，不是缩印本**：只有标题/简述/覆盖度/导航四节（详述节已删——
    # L1 由切片 L0 聚合生成，详述 ⊆ L0，是派生冗余），1600 对地图是宽裕的
    # 上限；切片内容走 L0，逐字细节走 L2，见 session_summarizer 的说明。
    overview_target_tokens: int = 1600
    abstract_tokens: int = 256
    # 一份概览最多用多少个切片作为输入（照 OpenViking 的 overview_sample_limit=32）。
    # 超限时按区间均匀抽样，首尾都保留。
    session_overview_sample_limit: int = 32
    # 宽会话的刷新阈值（照 OpenViking 的 freshness_refresh_ratio=0.10）：
    # 待跟上的变化占比达到它就重生成概览，否则只累计计数。
    # 切片数不超过 sampling limit 的会话不受此约束——那类会话重生成成本本来就低。
    session_overview_refresh_ratio: float = 0.10
    # 每次检索最多返回几条会话命中。刻意取小：会话是"目录"，正常一次只需要
    # 一两个入口，返回太多会把切片结果挤下去。
    session_top_k: int = 3
    # 会话重排的打分体预算（token）。L1 上限 4000，但打分体只取前 1200——
    # 判别力最强的简述 / 覆盖度 / 导航三段都在这之内（见 sessions.py 的说明）。
    session_rerank_max_tokens: int = 1200

    # 图扩散（设计文档 §16，v4）：从直接命中沿 links 图做 PPR，把多跳相连的
    # 切片**增补**进返回。直接命中永远原样在前，扩散只追加——所以默认开启
    # 不改变既有调用方看到的主结果，只是结果集后面可能多几条带 origin=expand 的。
    expand_enabled: bool = True
    # 最多增补几条扩散结果。刻意取小：它们是"可能相关"，多了会稀释上下文。
    expand_top_k: int = 3
    # 跳过触发信号、每次都扩散。评测/调试用（P0 评测复跑靠它），日常别开——
    # 大多数查询不需要扩散，常开只会白花 token。
    expand_always: bool = False

    # OpenAI 兼容记忆网关（采集原文 + 可选注入记忆）
    gateway_enabled: bool = False
    gateway_upstream_base_url: str = ""
    gateway_upstream_api_key: str = ""
    gateway_inject_memory: bool = True

    # 并发
    sqlite_wal: bool = True
    write_queue_size: int = 100

    # 分词
    tokenizer_extra_words: list[str] = field(default_factory=list)
    tokenizer_user_dict: str = ""
    tokenizer_stopwords_enabled: bool = True

    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        """数据目录一律绝对化——它决定库文件路径怎么写进 `registry.json`。

        相对路径（默认 `./data`，或 .env 里的 `DATA_DIR=./data`）会被原样登记，
        之后再被**别的进程**按各自的 cwd 解析。MCP 子进程与 hook 的 cwd 由宿主
        决定（通常是用户当时打开的项目目录），在那里 `data/work.db` 指向一个
        不存在的位置，库直接打不开——而 hook 的失败是静默的（退出码恒为 0）。
        """
        self.data_dir = Path(self.data_dir).expanduser().resolve()

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> Settings:
        """读取 .env（若存在）后构造配置。"""
        if env_file is not None:
            load_dotenv(env_file, override=False)
        else:
            candidate = Path.cwd() / ".env"
            if candidate.exists():
                load_dotenv(candidate, override=False)
            else:
                load_dotenv(override=False)

        settings = cls(
            backend_port=_int("BACKEND_PORT", 8001),
            frontend_port=_int("FRONTEND_PORT", 5174),
            data_dir=Path(_str("DATA_DIR", "./data")),
            default_db=_str("DEFAULT_DB", "duramem"),
            embedding_api_key=_str("EMBEDDING_API_KEY"),
            embedding_base_url=_str("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1"),
            embedding_model=_str("EMBEDDING_MODEL", "BAAI/bge-m3"),
            embedding_dim=_int("EMBEDDING_DIM", 1024),
            embedding_fake=_bool("EMBEDDING_FAKE", False),
            embed_batch_size=_int("EMBED_BATCH_SIZE", 32),
            embedding_send_dimensions=_bool("EMBEDDING_SEND_DIMENSIONS", False),
            rerank_enabled=_bool("RERANK_ENABLED", False),
            rerank_api_key=_str("RERANK_API_KEY"),
            rerank_base_url=_str("RERANK_BASE_URL", "https://api.siliconflow.cn/v1"),
            rerank_model=_str("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"),
            summary_api_key=_str("SUMMARY_API_KEY"),
            summary_base_url=_str("SUMMARY_BASE_URL"),
            summary_model=_str("SUMMARY_MODEL", "deepseek-chat"),
            main_model_api_key=_str("MAIN_MODEL_API_KEY"),
            main_model_base_url=_str("MAIN_MODEL_BASE_URL"),
            main_model=_str("MAIN_MODEL"),
            vector_top_k=_int("VECTOR_TOP_K", 20),
            lexical_top_k=_int("LEXICAL_TOP_K", 20),
            fusion_pool=_int("FUSION_POOL", 30),
            rrf_k=_int("RRF_K", 60),
            rerank_top_k=_int("RERANK_TOP_K", 5),
            vector_max_distance=_float("VECTOR_MAX_DISTANCE", 0.9),
            vector_weak_distance=_float("VECTOR_WEAK_DISTANCE", 0.75),
            vector_chunk_size=_int("VECTOR_CHUNK_SIZE", 64),
            auto_summary_enabled=_bool("AUTO_SUMMARY_ENABLED", False),
            summary_interval=_int("SUMMARY_INTERVAL", 30),
            summary_min_messages=_int("SUMMARY_MIN_MESSAGES", 4),
            summary_max_chunks=_int("SUMMARY_MAX_CHUNKS", 8),
            overview_access_enabled=_bool("OVERVIEW_ACCESS_ENABLED", True),
            # 旧键 L1_ACCESS_ENABLED 的含义是"开放原文回查"，与新的 L2 开关
            # 语义一致，所以作为回退读它——已部署的 .env 不会在升级后失效。
            original_access_enabled=_bool(
                "ORIGINAL_ACCESS_ENABLED", _bool("L1_ACCESS_ENABLED", True)
            ),
            # 新键名优先；兼容旧键 READ_BUDGET_TOKENS，让已写进 .env 的部署
            # 在升级后不至于把用户设的值丢掉。旧默认 4000 是硬限额，现在
            # 只当软阈值用，所以升级后行为从"会被拒绝"变成"只提示"。
            read_soft_limit_tokens=_int(
                "READ_SOFT_LIMIT_TOKENS", _int("READ_BUDGET_TOKENS", 0)
            ),
            read_budget_idle_reset=_int("READ_BUDGET_IDLE_RESET", 120),
            default_window_expand=_int("DEFAULT_WINDOW_EXPAND", 1),
            summary_target_tokens=_int("SUMMARY_TARGET_TOKENS", 180),
            summary_soft_limit_tokens=_int("SUMMARY_SOFT_LIMIT_TOKENS", 256),
            overview_target_tokens=_int("OVERVIEW_TARGET_TOKENS", 4000),
            abstract_tokens=_int("ABSTRACT_TOKENS", 256),
            session_overview_sample_limit=_int("SESSION_OVERVIEW_SAMPLE_LIMIT", 32),
            session_overview_refresh_ratio=_float("SESSION_OVERVIEW_REFRESH_RATIO", 0.10),
            session_top_k=_int("SESSION_TOP_K", 3),
            session_rerank_max_tokens=_int("SESSION_RERANK_MAX_TOKENS", 1200),
            expand_enabled=_bool("EXPAND_ENABLED", True),
            expand_top_k=_int("EXPAND_TOP_K", 3),
            expand_always=_bool("EXPAND_ALWAYS", False),
            gateway_enabled=_bool("GATEWAY_ENABLED", False),
            gateway_upstream_base_url=_str("GATEWAY_UPSTREAM_BASE_URL"),
            gateway_upstream_api_key=_str("GATEWAY_UPSTREAM_API_KEY"),
            gateway_inject_memory=_bool("GATEWAY_INJECT_MEMORY", True),
            sqlite_wal=_bool("SQLITE_WAL", True),
            write_queue_size=_int("WRITE_QUEUE_SIZE", 100),
            tokenizer_extra_words=_list("TOKENIZER_EXTRA_WORDS"),
            tokenizer_user_dict=_str("TOKENIZER_USER_DICT"),
            tokenizer_stopwords_enabled=_bool("TOKENIZER_STOPWORDS_ENABLED", True),
        )

        # 无 Key 且未显式指定时，自动启用离线伪向量，避免离线环境直接崩
        if not settings.embedding_api_key and not os.getenv("EMBEDDING_FAKE"):
            settings.embedding_fake = True

        settings.data_dir = Path(settings.data_dir)
        return settings

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # 便于调试：不打印密钥
    def redacted(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if "key" in f.name and value:
                out[f.name] = "***"
            else:
                out[f.name] = value
        return out


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def reset_settings() -> None:
    """测试用：清掉单例。"""
    global _settings
    _settings = None
