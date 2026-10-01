"""数据库 DDL 与初始化。

偏离设计文档 v2 的两处（实现时发现的必要调整）：

1. `chunks.search_text` 取代文档里的 `l0_seg`。
   FTS5 用 external-content 模式时，被索引的列必须与内容表同名同结构。
   文档把 `l0_seg` / `title` / `keywords` 列为三个 FTS 列，但 `title` 未分词、
   `keywords` 是 JSON，直接索引会污染词表。改为在应用层把三者合并分词后
   写入单列 `search_text`，FTS5 只索引这一列，触发器可自动维护。
   （代价：暂时无法用 FTS5 的 bm25() 做「标题命中加权」，需要时再拆列。）

2. 新增 `messages` 表（文档遗漏）。
   `read_original(mode="window")` 需要按 ±N 条消息扩展，
   `l2_text` 与 `l2_char_start/end` 需要可重新推导、可校验，
   都要求逐条原始消息落库。`chunks.msg_id_start/end` 指向本表 `id`。

## v2：三级分层

层级按容器/条目分开承载，仿 OpenViking 的 L0/L1/L2：

- **会话**（容器）→ `session_layers`：L0 摘要 + L1 概览。
  L1 是模型产出的概览（含覆盖度与导航），L0 由 L1 首段机械抽取派生。
- **切片**（条目）→ `chunks`：L0 摘要（检索匹配入口）+ L2 原文。

`chunks` 表**没有** `l1_text` 列——这与 OpenViking 一致（它不给文件建 sidecar）。
v1 的 `l1_text` 在这场改造里更名为 `l2_text`，`char_start/end` 更名为
`l2_char_start/end`（它们始终是相对原文层的偏移）。见 `migrate()`。

## v3：语义化命名（弃 L 编号）

L 编号表达的是"密度档位"，但结构是一棵树：L0 同时挂在会话与切片两种节点上
（OpenViking 自己也是如此），口语与配置里每次都要靠限定词消歧。v3 把四个产物
按"它是什么"命名，编号退役：

- 切片：`l0_text → summary_text`（摘要）、`l2_text → original_text`（原文）、
  `l0_tokens → summary_tokens`、`l0_truncated → summary_truncated`
- 会话：`l0_text → abstract_text`（简介）、`l1_text → overview_text`（概览）、
  `l0_tokens → abstract_tokens`、`l1_tokens → overview_tokens`、
  `l1_digest → overview_digest`

旧名 → 新名的完整对照记录在设计文档 A.1。见 `migrate()`。

## v4：切片间链接（`links` 表）

迭代检索环路的写入侧地基（设计文档 §16.7）：读取是迭代重建的，写入就要为
"未来能被重建到"负责——一条记忆必须有多条到达路径，否则扩散在第一跳就断。

- 行由 `store/links.py` 在切片写入/编辑路径上自动维护（被动链接：
  同会话邻接 + tags/keywords 交集，零模型成本）。
- `source='active'` 预留给将来摘要期的模型抽取，落地前没有写入方。
- 链接是**派生数据**，可整体重建（`duramem links-rebuild`）；真删除切片时
  物理清理，软删除靠查询侧活性过滤。
- 只新增表、不改列，所以老库升级靠 `_DDL_CORE` 的 `CREATE TABLE IF NOT
  EXISTS` 即可（`ensure_schema` 每次开库都跑），`migrate()` 只负责把
  `schema_version` 抬到 4。
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = "4"

# db_meta 的键（键值表，便于扩展）
META_KEYS = (
    "db_uuid",
    "created_at",
    "embedding_model",
    "embedding_dim",
    "reranker_model",
    "summary_soft_limit_tokens",
    "vector_chunk_size",
    "schema_version",
    # 向量来源指纹与作废标记。见 mark_vectors_stale 的说明。
    "vector_source",
    "vectors_stale",
)

# 记在库里的**实际**嵌入提供方（可能是 offline-hashing-1024，而不是配置里写的模型名）
META_VECTOR_SOURCE = "vector_source"
# "1" 表示库里的向量是上一个提供方生成的，与当前配置不可比
META_VECTORS_STALE = "vectors_stale"

_DDL_CORE = """
CREATE TABLE IF NOT EXISTS db_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 原始消息：采集适配器的落地点，也是原文回查与邻接扩展的依据
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,
    window_id     TEXT    NOT NULL,
    session_id    TEXT    NOT NULL,
    seq           INTEGER NOT NULL,          -- 在源窗口内的顺序
    role          TEXT    NOT NULL,          -- user / assistant / system
    speaker       TEXT,                      -- 角色名（SillyTavern 等场景）
    content       TEXT    NOT NULL,
    ts            TEXT,                      -- 源时间戳（可选）
    source_msg_id TEXT,                      -- 宿主侧的消息 ID（可选）
    content_hash  TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    UNIQUE (window_id, session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(window_id, session_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_hash    ON messages(content_hash);

-- 记忆切片（条目层）。纯文本 + 元数据，不含向量（向量在 chunks_vec）
--
-- 层级命名：summary_text 是检索匹配入口（每轮注入的唯一文本），original_text 是原文。
-- 没有概览列——概览是**会话**级概念，在 session_layers 表里。
-- original_char_start/end 始终是相对 original_text 的偏移（v1 叫 char_start/end，
-- v2 叫 l2_char_start/end）。
CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY,
    chunk_uid       TEXT    NOT NULL UNIQUE,   -- 对外句柄，创建后永不变
    title           TEXT    NOT NULL DEFAULT '',
    title_suggested TEXT,                      -- 模型建议，待用户确认
    summary_text    TEXT    NOT NULL,
    search_text     TEXT    NOT NULL DEFAULT '', -- 摘要+标题+关键词 的分词结果，供 FTS5
    original_text   TEXT    NOT NULL DEFAULT '',
    keywords        TEXT    NOT NULL DEFAULT '[]',
    source_db       TEXT,
    source_window   TEXT,
    source_session  TEXT,
    msg_id_start    INTEGER,
    msg_id_end      INTEGER,
    original_char_start INTEGER,
    original_char_end   INTEGER,
    content_hash    TEXT    NOT NULL,          -- 摘要的 SHA-256，库内去重用
    hit_count       INTEGER NOT NULL DEFAULT 0,
    weight          REAL    NOT NULL DEFAULT 1.0,
    tags            TEXT    NOT NULL DEFAULT '[]',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    deleted_at      TEXT,                      -- 软删除；NULL = 有效
    superseded_by   TEXT,                      -- 版本链：被哪条 chunk_uid 取代
    summary_tokens    INTEGER NOT NULL DEFAULT 0,
    summary_truncated INTEGER NOT NULL DEFAULT 0 -- 摘要是否触顶，供调试面板统计
);
CREATE INDEX IF NOT EXISTS idx_chunks_hash    ON chunks(content_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_created ON chunks(created_at);
CREATE INDEX IF NOT EXISTS idx_chunks_live    ON chunks(created_at)
    WHERE deleted_at IS NULL AND superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(source_window, source_session);

-- 会话层（容器层）= OpenViking 的目录 sidecar。
--
-- overview 是模型产出的概览，结构含四段：H1 标题 / Brief Description /
-- Directory Coverage（本会话切片总数、是否全量）/ Quick Navigation
-- （决策树，每条指向一个切片 uid）/ Detailed Description（逐切片小节）。
-- abstract 不是第二次模型调用——它由 overview 正文的首段机械抽取而来（跳过
-- H1 与 frontmatter，取到第一个二级标题之前），因此零额外生成成本。
--
-- pending_changes 照搬 OpenViking 的 pending_child_changes：累计"子切片变了但
-- 概览还没跟上"的事件数。刷新决策见 session_layers 的 freshness 策略。
CREATE TABLE IF NOT EXISTS session_layers (
    id                INTEGER PRIMARY KEY,
    window_id         TEXT    NOT NULL,
    session_id        TEXT    NOT NULL,
    abstract_text     TEXT    NOT NULL DEFAULT '',
    overview_text     TEXT    NOT NULL DEFAULT '',
    search_text       TEXT    NOT NULL DEFAULT '', -- 简介+标题 的分词结果，供 FTS5
    abstract_tokens   INTEGER NOT NULL DEFAULT 0,
    overview_tokens   INTEGER NOT NULL DEFAULT 0,
    model_used        TEXT,
    overview_digest   TEXT,                        -- 概览正文指纹，用于变更判定
    pending_changes   INTEGER NOT NULL DEFAULT 0,
    coverage_total    INTEGER NOT NULL DEFAULT 0,  -- 生成时的切片总数（覆盖度声明）
    coverage_sampled  INTEGER NOT NULL DEFAULT 0,  -- 实际送进 prompt 的切片数
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    UNIQUE (window_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_session_layers_updated ON session_layers(updated_at);

-- FTS5 外部内容表：与会话层同步（与会话向量路一起构成混合检索）
CREATE VIRTUAL TABLE IF NOT EXISTS session_layers_fts USING fts5(
    search_text,
    content='session_layers',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS session_layers_fts_ai AFTER INSERT ON session_layers BEGIN
    INSERT INTO session_layers_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;
CREATE TRIGGER IF NOT EXISTS session_layers_fts_ad AFTER DELETE ON session_layers BEGIN
    INSERT INTO session_layers_fts(session_layers_fts, rowid, search_text)
        VALUES ('delete', old.id, old.search_text);
END;
CREATE TRIGGER IF NOT EXISTS session_layers_fts_au AFTER UPDATE ON session_layers BEGIN
    INSERT INTO session_layers_fts(session_layers_fts, rowid, search_text)
        VALUES ('delete', old.id, old.search_text);
    INSERT INTO session_layers_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;

-- FTS5 外部内容表：只索引 search_text，由触发器与 chunks 同步
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    search_text,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, search_text)
        VALUES ('delete', old.id, old.search_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, search_text)
        VALUES ('delete', old.id, old.search_text);
    INSERT INTO chunks_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;

-- 会话级总结游标：防止两个窗口重复总结同一段对话
CREATE TABLE IF NOT EXISTS summary_cursors (
    window_id              TEXT    NOT NULL,
    session_id             TEXT    NOT NULL,
    last_summarized_msg_id INTEGER NOT NULL DEFAULT -1,
    updated_at             TEXT    NOT NULL,
    PRIMARY KEY (window_id, session_id)
);

-- 总结运行记录（供前端与调试面板查看）
CREATE TABLE IF NOT EXISTS summary_runs (
    id           INTEGER PRIMARY KEY,
    window_id    TEXT,
    session_id   TEXT,
    msg_id_start INTEGER,
    msg_id_end   INTEGER,
    chunks_added INTEGER NOT NULL DEFAULT 0,
    duplicates   INTEGER NOT NULL DEFAULT 0,
    rejected     INTEGER NOT NULL DEFAULT 0,
    warnings     TEXT NOT NULL DEFAULT '[]',
    model_used   TEXT,
    duration_ms  INTEGER,
    created_at   TEXT NOT NULL
);

-- 切片间链接（v4）：迭代检索环路图扩散的边。见模块头注释与 store/links.py。
-- 内部用 chunks.id（整数、快），对外句柄 chunk_uid 靠 JOIN 取。
CREATE TABLE IF NOT EXISTS links (
    id         INTEGER PRIMARY KEY,
    src_id     INTEGER NOT NULL,           -- chunks.id
    dst_id     INTEGER NOT NULL,           -- chunks.id
    relation   TEXT    NOT NULL,           -- adjacent / tag / keyword / (预留 active)
    source     TEXT    NOT NULL DEFAULT 'passive',  -- passive=规则生成, active=模型抽取（预留）
    weight     REAL    NOT NULL DEFAULT 1.0,
    created_at TEXT    NOT NULL,
    UNIQUE (src_id, dst_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_id);
CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst_id);
"""

_VDDL_VEC = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    chunk_id  INTEGER PRIMARY KEY,
    embedding FLOAT[{dim}] distance_metric=cosine,
    chunk_size={chunk_size}
);
"""

# 会话层的向量表。独立 id 空间（layer_id 指向 session_layers.id）——不能塞进
# chunks_vec，那张表的主键是 chunks.id，两者混用会让"按 id 取切片"取到会话行。
_VDDL_SESSION_VEC = """
CREATE VIRTUAL TABLE IF NOT EXISTS session_layers_vec USING vec0(
    layer_id  INTEGER PRIMARY KEY,
    embedding FLOAT[{dim}] distance_metric=cosine,
    chunk_size={chunk_size}
);
"""

# 用余弦距离而非 sqlite-vec 默认的 L2。原因：距离值变得可解释
# （0 = 同向，1 = 正交，2 = 反向），于是"相关性下限"这个配置项才有意义，
# 也才能在检索调试面板里被人读懂。BGE-M3 输出已归一化，余弦是合适度量。
#
# `chunk_size` 是**表级**选项（不是列选项），控制向量存储的分配块大小。
# sqlite-vec 默认 1024，即 1024 条 × 1024 维 × 4 字节 = **正好 4 MB 的固定下限**，
# 与库里实际有几条向量无关。实测（20000 条 1024 维，KNN 20 次取平均）：
#   chunk_size=16   -> 23.98 ms/次
#   chunk_size=64   -> 22.42 ms/次
#   chunk_size=256  -> 21.86 ms/次
#   chunk_size=1024 -> 22.11 ms/次
# 差异在噪声内，稳态占用也相同（79 MB）。也就是说 chunk_size 只影响固定下限，
# 不影响检索性能——所以取小值。64 时下限约 288 KB，是默认值的 1/14。
# 多库设计下这个差别会翻倍，值得。
DEFAULT_VECTOR_CHUNK_SIZE = 64


class EmbeddingMismatchError(RuntimeError):
    """库内向量模型/维度与当前配置不一致。

    不允许静默继续：跨模型比较向量距离会返回看似有分数、实则全是噪声的结果。
    """


def create_vector_table(
    conn: sqlite3.Connection, dim: int, chunk_size: int = DEFAULT_VECTOR_CHUNK_SIZE
) -> None:
    conn.execute(_VDDL_VEC.format(dim=int(dim), chunk_size=int(chunk_size)))


def drop_vector_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS chunks_vec")


def create_session_vector_table(
    conn: sqlite3.Connection, dim: int, chunk_size: int = DEFAULT_VECTOR_CHUNK_SIZE
) -> None:
    conn.execute(_VDDL_SESSION_VEC.format(dim=int(dim), chunk_size=int(chunk_size)))


def drop_session_vector_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS session_layers_vec")


def mark_vectors_stale(conn: sqlite3.Connection, source: str) -> None:
    """把库里的向量标记为"由 source 生成，与当前配置不可比"。

    为什么需要这个标记，而不是只比对模型名：`embedding_model` 记的是
    配置里写的名字，而**实际**生成向量的实现可能是离线哈希（没填 Key 时
    会自动退到它）。于是"配置 bge-m3 + 库里记着 bge-m3 + 向量其实是哈希"
    这种组合下，纯比名字会一路放行，用户填上真 Key 后新老向量就静默混在一起，
    检索结果是无法解释的噪声。

    标记在**换模型的那一刻**写入，因为只有那一刻我们才知道上一个生效的
    提供方是谁——这是唯一能可靠拿到的信息。
    """
    update_meta(conn, **{META_VECTOR_SOURCE: source, META_VECTORS_STALE: "1"})


def clear_vectors_stale(
    conn: sqlite3.Connection, source: str, model: str, dim: int
) -> None:
    """重建向量后记录新的来源，并解除作废标记。"""
    update_meta(
        conn,
        **{
            META_VECTOR_SOURCE: source,
            META_VECTORS_STALE: "0",
            "embedding_model": model,
            "embedding_dim": str(dim),
        },
    )


def update_meta(conn: sqlite3.Connection, **values: str) -> None:
    conn.executemany(
        "INSERT INTO db_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [(key, str(value)) for key, value in values.items()],
    )


# 列改名表。为什么用 PRAGMA 探测实际列名而不是读 schema_version：老库的
# db_meta 里可能根本没有这个键——`init_database` 只在 db_meta 为**空**时写入
# 初始键，所以老库只在建库那一刻写过，之后新增的键（含 schema_version 引入
# 之前的库）不会补写。依赖版本号会漏掉这些库；探测列名则无论库多老都成立。
#
# 元组按历史顺序排列，同一列的多次改名靠"改完刷新列清单"串成链：
# v1 库走 l1_text → l2_text → original_text，v2 库从 l2_text 起，v3 库跳过全部。
_CHUNKS_RENAMES: tuple[tuple[str, str], ...] = (
    # v1→v2：v1 的 l1_text 是原文，v2 里原文叫 L2（L1 是会话级的概览层）
    ("l1_text", "l2_text"),
    # 这两个偏移始终相对原文层，跟着改名以免读代码的人以为它们指向会话概览
    ("char_start", "l2_char_start"),
    ("char_end", "l2_char_end"),
    # v2→v3：语义化命名（见模块 docstring 的 v3 段）
    ("l0_text", "summary_text"),
    ("l2_text", "original_text"),
    ("l2_char_start", "original_char_start"),
    ("l2_char_end", "original_char_end"),
    ("l0_tokens", "summary_tokens"),
    ("l0_truncated", "summary_truncated"),
)

_SESSION_LAYERS_RENAMES: tuple[tuple[str, str], ...] = (
    # v2→v3：session_layers 表 v2 才引入，只有这一段历史
    ("l0_text", "abstract_text"),
    ("l1_text", "overview_text"),
    ("l0_tokens", "abstract_tokens"),
    ("l1_tokens", "overview_tokens"),
    ("l1_digest", "overview_digest"),
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """把已存在的库升级到当前 schema。幂等，可重复执行。

    为什么必须有它：`CREATE TABLE IF NOT EXISTS` 对**已存在**的表既不会加列
    也不会改名，所以存量库光靠 `_DDL_CORE` 永远升不上来。这是本项目的第一个
    迁移，没有先例可抄。

    三个进程会各自打开同一个库（MCP 子进程 / hook / serve），所以它在每次
    `init_database` 时都要跑，且重复执行必须无副作用。

    返回本次实际做过的动作，供测试与日志使用（空列表 = 无需迁移）。
    """
    actions: list[str] = []

    for table, renames in (
        ("chunks", _CHUNKS_RENAMES),
        ("session_layers", _SESSION_LAYERS_RENAMES),
    ):
        for old, new in renames:
            cols = _table_columns(conn, table)
            # 两个条件都要查。只看 old 存在的话，在"迁移到一半崩掉"的库上会重复
            # 改名并报错；只看 new 不存在则会在全新的空库上误判。
            # 列清单**每轮重读**：同一列在历史里被改过多次名（l1_text→l2_text→
            # original_text），上一轮改名必须让下一轮看见，链才能续上。
            if old in cols and new not in cols:
                conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
                actions.append(f"{table}.{old} -> {new}")

    # 记录版本。老库可能压根没有这个键，所以不看旧值是否"更小"，只看当前是否
    # 已到 v3；已记录过的库不再重复写，避免每次打开都产生一次无意义写入。
    if actions or read_meta(conn).get("schema_version") != SCHEMA_VERSION:
        update_meta(conn, schema_version=SCHEMA_VERSION)

    return actions


def ensure_schema(
    conn: sqlite3.Connection,
    fallback_dim: int = 0,
    fallback_chunk_size: int = DEFAULT_VECTOR_CHUNK_SIZE,
) -> list[str]:
    """确保库的结构是当前版本：建表、迁移、建向量表。幂等。

    **每次打开库都要跑，不能只在建库时跑。** 这一点曾搞错过：迁移最初挂在
    `init_database` 上，而 `Database.initialize` 只在**建库**时被调用
    （`registry.create`），正常打开走的是 `registry.open_one`——于是存量库
    打开后仍是旧列名，迁移形同虚设。实测才发现。

    三个进程会各自打开同一个库（MCP 子进程 / hook / serve），谁先打开谁就得
    把它升上来，否则后开的进程会读到半新半旧的表结构。

    返回迁移动作清单（空列表 = 无需迁移）。
    """
    conn.executescript(_DDL_CORE)
    actions = migrate(conn)

    # 向量表维度由**库内记录**决定，不由当前配置决定——配置换了模型也不该
    # 悄悄改掉已有向量表的形状（那会让存量向量失去可比性，而这个判断由
    # `assert_embedding_compatible` 专门负责报错）。
    meta = read_meta(conn)
    dim = int(meta.get("embedding_dim") or fallback_dim or 0)
    chunk_size = int(meta.get("vector_chunk_size") or fallback_chunk_size)
    if dim > 0:
        create_vector_table(conn, dim, chunk_size)
        create_session_vector_table(conn, dim, chunk_size)
    return actions


def init_database(
    conn: sqlite3.Connection,
    db_uuid: str,
    created_at: str,
    embedding_model: str,
    embedding_dim: int,
    summary_soft_limit_tokens: int,
    reranker_model: str = "",
    vector_chunk_size: int = DEFAULT_VECTOR_CHUNK_SIZE,
) -> None:
    """建库：写入初始 db_meta，然后确保结构为当前版本。

    顺序刻意如此：**先写初始 meta，再 ensure_schema**。反过来的话，
    ensure_schema 里的 migrate 会先写入 schema_version 使 db_meta 变成非空，
    下面那个 `if not existing:` 就被跳过——db_uuid / embedding_model /
    embedding_dim 全都不写，库当场残废。
    """
    conn.executescript(_DDL_CORE)

    existing = dict(conn.execute("SELECT key, value FROM db_meta").fetchall())

    if not existing:
        conn.executemany(
            "INSERT INTO db_meta(key, value) VALUES (?, ?)",
            [
                ("db_uuid", db_uuid),
                ("created_at", created_at),
                ("embedding_model", embedding_model),
                ("embedding_dim", str(embedding_dim)),
                ("reranker_model", reranker_model),
                ("summary_soft_limit_tokens", str(summary_soft_limit_tokens)),
                ("vector_chunk_size", str(vector_chunk_size)),
                ("schema_version", SCHEMA_VERSION),
            ],
        )

    ensure_schema(conn, fallback_dim=embedding_dim, fallback_chunk_size=vector_chunk_size)


def read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM db_meta").fetchall())


def assert_embedding_compatible(
    conn: sqlite3.Connection,
    embedding_model: str,
    embedding_dim: int,
    embedding_provider: str = "",
) -> None:
    """检索/写入前校验。不一致直接抛错。

    `embedding_provider` 是**实际生效**的提供方标识（离线时形如
    `offline-hashing-1024`）。它为空时退化成只比对模型名与维度——
    那是旧版本库的行为，保留以便不破坏已存在的库。
    """
    meta = read_meta(conn)
    stored_model = meta.get("embedding_model") or ""
    stored_dim = int(meta.get("embedding_dim") or 0)
    source = (meta.get(META_VECTOR_SOURCE) or "").strip()
    provider = (embedding_provider or "").strip()

    problems: list[str] = []
    if stored_model and embedding_model and stored_model != embedding_model:
        problems.append(f"库内向量模型={stored_model}，当前配置={embedding_model}")
    if stored_dim and embedding_dim and stored_dim != embedding_dim:
        problems.append(f"库内维度={stored_dim}，当前配置={embedding_dim}")

    # 来源指纹对不上：库里那批向量是别的实现生成的，与当前生效的不可比。
    #
    # 这里**不要求 vectors_stale 标记**。标记由"改配置的那个进程"写；别的进程
    # （MCP 子进程、hook）在重启前根本不知道配置变了。只看标记的话，这种进程会
    # 一路放行——它用哈希算查询向量、去比真模型的向量，距离全落在阈值之外，
    # 整个向量路被静默剔空、退化成纯词法检索，而界面上一片正常。指纹就是为识别
    # 这种"模型名一样、实际提供方不同"而存在的，不该被标记挡住。
    if source and provider and source != provider:
        problems.append(
            f"库内向量由 {source} 生成，当前生效的是 {provider}"
            "（此时旧向量与新向量不可比，混在一起检索只会得到噪声）"
        )

    if problems:
        raise EmbeddingMismatchError(
            "向量模型与库不匹配，已拒绝操作："
            + "；".join(problems)
            + "。请改用匹配的模型，或在界面上对这张库执行「重建向量」"
            "（等价命令：`duramem rebuild-vectors --db <库名>`）。"
            "若刚改过模型配置，还需重启正在运行的服务/会话——"
            "长驻进程（MCP 子进程、serve）的配置是启动时读入的。"
        )
