"""共享 HTTP 传输：缓存、惰性、单例。

守的是性能契约，不是实现细节——这几条断言背后的数字见 duramem/net.py 的
模块说明与设计文档 A.17：

- 上下文必须缓存：每次重建要 ~900ms（解析 certifi 的 235KB CA bundle）
- 客户端必须惰性：构造提供方不该建客户端，否则"打开一个库"这条路径
  会替一份可能用不上的传输付 4 次客户端构造（实测 12.6s）
- 超时必须按请求传：各家语义不同（60/120/180s），在客户端上定死会静默改掉约束
"""

from __future__ import annotations

import ssl

import pytest

from duramem import net


@pytest.fixture(autouse=True)
def _clean_shared_client():
    """每个用例前后都从干净状态开始，避免单例把用例串起来。"""
    net.close_shared_client()
    yield
    net.close_shared_client()


class _FakeResponse:
    status_code = 200

    def json(self) -> dict:
        return {"data": [{"index": 0, "embedding": [0.0] * 4}], "results": []}

    text = ""


class _RecordingClient:
    """记下每次请求的 timeout，供断言"超时是按请求传的"。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse()


# ====================================================================== 缓存


def test_ssl_context_is_cached():
    """反复取必须是同一个对象——每次重建 ~900ms，缓存是这次修复的支点。"""
    first = net.ssl_context()
    second = net.ssl_context()
    assert first is second
    assert isinstance(first, ssl.SSLContext)


def test_client_is_a_singleton():
    assert net.shared_client() is net.shared_client()


def test_close_then_get_again_rebuilds_without_hanging():
    """回归：`shared_client()` 曾在自己的锁里调 `ssl_context()`（它也要同一把
    Lock），普通 Lock 不可重入 → 自锁死，调用永久挂起、无异常无输出。
    这条用例的价值就在"能返回"本身。
    """
    first = net.shared_client()
    net.close_shared_client()
    assert net._client is None, "close 之后单例必须清空"
    second = net.shared_client()
    assert second is not first
    assert not second.is_closed


def test_getting_a_client_does_not_rebuild_the_context():
    """共享的客户端必须复用缓存上下文。

    httpx 把 verify 的上下文留在 transport 的连接池里，不保证能读回来，
    所以这里断言可观测的那一面：取客户端不会另建一个上下文对象。
    """
    before = net.ssl_context()
    net.close_shared_client()
    net.shared_client()
    assert net.ssl_context() is before


# ====================================================================== 惰性


def test_building_providers_does_not_build_a_client():
    """构造四个提供方不该产生任何客户端。

    这是本次性能修复的载荷点：从前每个提供方在 __init__ 里建 httpx.Client，
    四次构造 × 每次 2-3 个 SSL 上下文 ≈ 12.6s，全部压在 Service._components()
    这条"打开库"路径上——而 hook 路径在关掉自动摘要时根本不调模型。
    """
    from duramem.providers.embedding import OpenAICompatibleEmbedding
    from duramem.providers.rerank import OpenAICompatibleReranker
    from duramem.session_summarizer import OpenAICompatibleOverviewProvider
    from duramem.summarizer import OpenAICompatibleSummarizer

    assert net._client is None
    OpenAICompatibleEmbedding("https://api.example.test/v1", "sk-x", "m", 1024)
    OpenAICompatibleReranker("https://api.example.test/v1", "sk-x", "m")
    OpenAICompatibleSummarizer("https://api.example.test/v1", "sk-x", "m")
    OpenAICompatibleOverviewProvider("https://api.example.test/v1", "sk-x", "m")
    assert net._client is None, "构造期不得创建客户端（应为首次发请求时才建）"


def test_components_do_not_build_a_client(settings):
    """端到端那条：打开一个库（_components）不该建 HTTP 客户端。"""
    from duramem.service import Service

    service = Service(settings)
    service.create_database("work")
    try:
        service._components("work")
        assert net._client is None, "打开库不该为模型传输付出任何代价"
    finally:
        service.close()


# ====================================================================== 超时


def test_embedding_passes_its_own_timeout_per_request(monkeypatch):
    from duramem.providers.embedding import OpenAICompatibleEmbedding

    recorder = _RecordingClient()
    monkeypatch.setattr(net, "shared_client", lambda: recorder)

    embedder = OpenAICompatibleEmbedding(
        "https://api.example.test/v1", "sk-x", "m", 4, timeout=7.5
    )
    embedder.embed_one("文本")

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["timeout"] == 7.5, "超时必须按请求传，不能在客户端上定死"
    assert recorder.calls[0]["url"].endswith("/embeddings")


def test_rerank_passes_its_own_timeout_per_request(monkeypatch):
    from duramem.providers.rerank import OpenAICompatibleReranker

    recorder = _RecordingClient()
    monkeypatch.setattr(net, "shared_client", lambda: recorder)

    reranker = OpenAICompatibleReranker(
        "https://api.example.test/v1", "sk-x", "m", timeout=3.25
    )
    reranker.rerank("q", ["a", "b"])

    assert recorder.calls[0]["timeout"] == 3.25
    assert recorder.calls[0]["url"].endswith("/rerank")


def test_summarizer_passes_its_own_timeout_per_request(monkeypatch, settings):
    """摘要的超时最长（默认 120s），最容易被"客户端上定死一个默认值"悄悄改掉。"""
    from duramem.summarizer import OpenAICompatibleSummarizer

    recorder = _RecordingClient()
    monkeypatch.setattr(net, "shared_client", lambda: recorder)

    summarizer = OpenAICompatibleSummarizer(
        "https://api.example.test/v1", "sk-x", "m", timeout=123.0
    )
    # 假响应结构不完整，解析阶段会抛；这里只关心请求有没有发出去，
    # 以及发出去时带的超时是不是提供方自己的那个。消息要带 id——
    # `_format_transcript` 按 `[id] role: content` 渲染，真实调用给的是库里的行。
    with pytest.raises(Exception):  # noqa: B017
        summarizer.summarize(
            [{"id": 1, "role": "user", "content": "x"}], 1, settings
        )

    assert recorder.calls, "请求必须已经发出（否则下面的断言只是被异常掩盖）"
    assert recorder.calls[0]["timeout"] == 123.0
