"""共享 HTTP 传输：进程级单例客户端 + 缓存的 SSL 上下文。

为什么需要它（2026-09-29 实测，Windows / httpx 0.28.1）：

httpx 的 `create_ssl_context()` 内部调的是
`ssl.create_default_context(cafile=certifi.where())` —— 也就是**每次都要把那个
235KB 的 CA bundle 完整解析一遍**。开发机上实测 `load_verify_locations` 单次
**895–918 ms**（同一个调用不带 cafile、走系统证书库只要 30 ms），而构造一个
`httpx.Client()` 要建 2–3 个 SSL 上下文。

后果是 `Service._components()` 里四个提供方各建一个客户端 → 12 次上下文构造 →
**12.6 秒**挂在"打开一个库"这条路径上。hook 每次事件都是新进程，于是每个事件
都要付一次 12.6 秒；设计文档决策 T 记的 +0.9s 只算了 jieba，漏了这一段
（见附录 A.17）。

这个模块只做两件事，都是把已经要建的东西缓存起来，不改任何请求语义：

1. `ssl_context()`：进程内只构造一次上下文，所有客户端共用；
2. `shared_client()`：**首次真正发请求时**才创建的单例 `httpx.Client`。

第 2 条顺带修掉一处隐性浪费：hook 路径在摘要自动触发关闭时根本不调模型，
但从前的构造流程照样会把四个客户端建出来——为一份从不使用的传输付 12.6 秒。
改成"用的时候才取客户端"之后，不调模型就一分钱不花。

刻意的取舍（没做的，以及为什么）：

- **信任来源不变**：仍然用 certifi 的 bundle，与改动前一致。改用系统证书库能再
  省 ~0.9s/进程，但它会改 TLS 信任来源（企业代理解密、系统根证书都会影响结果），
  属于独立决策，不混进这次性能修复。
- **共享的只是传输与连接池，不是请求**：每个提供方仍自己发请求，超时按请求传
  （各家不同：嵌入/重排 60s、摘要 120s、概览 180s、探测 30s），客户端上的默认值
  只是兜底，避免漏传时无限挂起。
- **异步客户端刻意不共享**：`AsyncClient` 绑定创建它的 event loop，跨 loop 复用会炸
  （`TestClient` 每个实例一个 loop，测试会互相污染）。网关只取共享的 SSL 上下文，
  每次请求仍自建客户端——那一笔开销因此从 3.4s 降到亚毫秒，剩下的连接复用
  以后需要时再单独做。
"""

from __future__ import annotations

import ssl
import threading

import certifi
import httpx

# 漏传 timeout 时的兜底值。真实超时一律按请求传——各提供方的语义不同，
# 在客户端上定死一个会让"摘要最长 120s"这类约束被静默改掉。
DEFAULT_TIMEOUT = 60.0

_ssl_context: ssl.SSLContext | None = None
_client: httpx.Client | None = None
_lock = threading.Lock()


def ssl_context() -> ssl.SSLContext:
    """进程级缓存的 SSL 上下文（双检锁，REST/MCP 侧会并发调用）。"""
    global _ssl_context
    if _ssl_context is None:
        with _lock:
            if _ssl_context is None:
                _ssl_context = ssl.create_default_context(cafile=certifi.where())
    return _ssl_context


def shared_client() -> httpx.Client:
    """进程级单例 `httpx.Client`。

    httpx 的客户端是线程安全的（内部连接池自带锁），REST 侧的线程池与
    MCP 侧共用同一个实例即可。

    注意上下文要在拿 `_lock` **之前**取：`_lock` 是普通 Lock 不是 RLock，
    在锁里再调 `ssl_context()`（它自己也要拿同一把锁）会自锁死。
    """
    global _client
    if _client is None:
        context = ssl_context()
        with _lock:
            if _client is None:
                _client = httpx.Client(timeout=DEFAULT_TIMEOUT, verify=context)
    return _client


def close_shared_client() -> None:
    """关掉单例，下次调用会重新建。

    测试用它做隔离；生产路径不调用——共享客户端属于进程而非某个库，
    在 `Service.close()` 里关会误伤同进程里还在用的其他库。
    进程退出时由解释器回收套接字。
    """
    global _client
    with _lock:
        client, _client = _client, None
    if client is not None:
        client.close()


__all__ = ["DEFAULT_TIMEOUT", "close_shared_client", "shared_client", "ssl_context"]
