"""测试夹具。全部离线运行：不需要任何 API Key，不访问网络。

为什么能做到：`HashingEmbedding` / `OverlapReranker` / `HeuristicSummarizer`
是确定性实现，它们的相似度是真实的（共享词项越多余弦越高），
因此"错误码查询应把含错误码的切片排到第一"这类断言是有意义的，
不是为了让测试通过而写的假断言。
"""

from __future__ import annotations

import pytest

from duramem.config import Settings
from duramem.models import Message
from duramem.service import Service
from duramem.text.tokenize import reset_segmenter


@pytest.fixture(autouse=True)
def _clean_segmenter():
    reset_segmenter()
    yield
    reset_segmenter()


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
