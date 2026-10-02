"""测试夹具。全部离线运行：不需要任何 API Key，不访问网络。

为什么能做到：`HashingEmbedding` / `OverlapReranker` / `HeuristicSummarizer`
是确定性实现，它们的相似度是真实的（共享词项越多余弦越高），
因此"错误码查询应把含错误码的切片排到第一"这类断言是有意义的，
不是为了让测试通过而写的假断言。
"""

from __future__ import annotations

import pytest
from jieba import finalseg

from duramem.config import Settings
from duramem.models import Message
from duramem.service import Service
from duramem.text import tokenize
from duramem.text.tokenize import Segmenter, get_segmenter

# ====================================================================== 分词隔离
# 进程内只建一次基线 Segmenter，每个用例前后恢复快照：内容级隔离（词典与全新
# 实例一致）但不重复重建词典。生产 reset_segmenter() 契约不变；隔离语义由
# tests/test_segmenter_isolation.py 锁定。

# (FREQ, total, user_word_tag_tab, _dynamic, finalseg.Force_Split_Words)
_SegmenterSnapshot = tuple[dict[str, int], float, dict[str, str], set[str], set[str]]


def _snapshot_segmenter(seg: Segmenter) -> _SegmenterSnapshot:
    """抓取分词注册涉及的状态（测试用）。

    - `_tk.FREQ`：add_word 给词的每个前缀补 FREQ=0 占位（jieba/__init__.py:432-435），
      get_DAG 靠 `frag not in FREQ` 提前断开，占位不能残留
    - `_tk.total`：add_word 按 freq 累加，log(total) 是分词归一化基准
    - `_tk.user_word_tag_tab`：add_word 带 tag 时写入；现实现不传 tag，防御性纳入
    - `_dynamic`：已注册的受保护 token 集
    - `finalseg.Force_Split_Words`：模块级集合，只有 add 没 remove；
      del_word（= add_word(freq=0)，:436-437）会把词永久塞进去，防御性纳入
    """
    return (
        dict(seg._tk.FREQ),
        seg._tk.total,
        dict(seg._tk.user_word_tag_tab),
        set(seg._dynamic),
        set(finalseg.Force_Split_Words),
    )


def _restore_segmenter(seg: Segmenter, snap: _SegmenterSnapshot) -> None:
    """把快照原样写回 Segmenter（clear+update 保持 dict/set 对象身份不变）。"""
    freq, total, tag_tab, dynamic, force_split = snap
    seg._tk.FREQ.clear()
    seg._tk.FREQ.update(freq)
    seg._tk.total = total
    seg._tk.user_word_tag_tab.clear()
    seg._tk.user_word_tag_tab.update(tag_tab)
    seg._dynamic.clear()
    seg._dynamic.update(dynamic)
    finalseg.Force_Split_Words.clear()
    finalseg.Force_Split_Words.update(force_split)


# (基线 Segmenter, 基线快照)。jieba.Tokenizer 懒加载：首次分词才构建词典，
# 基线必须显式 initialize() 后再快照，否则快照到的是空词典。
_segmenter_baseline: tuple[Segmenter, _SegmenterSnapshot] | None = None


def _baseline_segmenter() -> tuple[Segmenter, _SegmenterSnapshot]:
    global _segmenter_baseline
    if _segmenter_baseline is None:
        seg = get_segmenter()
        seg._tk.initialize()
        _segmenter_baseline = (seg, _snapshot_segmenter(seg))
    return _segmenter_baseline


@pytest.fixture(autouse=True)
def _clean_segmenter():
    """每个用例前后把分词器恢复成基线：上一用例注册的动态 token、词频漂移不泄漏。
    用例中途调用生产 reset_segmenter() 照常生效（置 None、下次重建），夹具收尾兜底还原。"""
    seg, snap = _baseline_segmenter()
    _restore_segmenter(seg, snap)
    tokenize._segmenter = seg
    yield
    _restore_segmenter(seg, snap)
    tokenize._segmenter = seg


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.embedding_fake = True
    s.rerank_enabled = False
    s.read_soft_limit_tokens = 0  # 生产默认：不设限，读多少给多少
    s.read_budget_idle_reset = 3600
    s.auto_summary_enabled = False
    s.summary_min_messages = 2
    s.summary_soft_limit_tokens = 256
    # 老测试断言的是 v3 检索行为（retrieval_mode / 命中集）。
    # 图扩散的默认值是开的，但它的行为契约（增补不替换、触发信号、模式标注）
    # 在 test_links.py 里显式开启并单独验证——这里关掉保持既有断言的语义。
    s.expand_enabled = False
    return s


@pytest.fixture
def service(settings) -> Service:
    svc = Service(settings)
    svc.create_database("work")
    yield svc
    svc.close()


# ====================================================================== 语料

DIALOGUE = [
    ("user", "我这边后端报 ERR_CONN_REFUSED_0x7f，不知道咋回事"),
    ("assistant", "连接被拒绝。先看端口 BACKEND_PORT 是不是被占了，默认 8001"),
    ("user", "怎么看端口占用"),
    ("assistant", "netstat -ano | findstr 8001，找到 PID 再 taskkill /PID <pid> /F"),
    ("user", "换成 9000 了"),
    ("assistant", "对，改 .env 里的 BACKEND_PORT=9000 然后重启，问题解决"),
    ("user", "另外许可证那边有什么坑"),
    ("assistant", "避开 AGPL 和 BSL 的依赖，jina-reranker-v2 是 CC-BY-NC 非商用，建议用 bge-reranker-v2-m3"),
    ("user", "向量库用什么"),
    ("assistant", "用 sqlite-vec 走进程内，配合 FTS5 做词法，RRF 融合两路。规模超 20 万条再换 zvec"),
]

WINDOW = "win-1"
SESSION = "chat-A"


def dialogue_messages(window: str = WINDOW, session: str = SESSION) -> list[Message]:
    return [
        Message(window, session, index, role, content, speaker="我" if role == "user" else "助手")
        for index, (role, content) in enumerate(DIALOGUE)
    ]


@pytest.fixture
def seeded(service: Service) -> Service:
    """采集对话并完成一次总结。"""
    service.ingest(dialogue_messages(), db="work", auto_summarize=False)
    outcome = service.summarize(WINDOW, SESSION, db="work")
    assert outcome.ok, outcome.warnings
    return service
