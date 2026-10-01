"""向量模型提供方。

统一走 OpenAI 兼容的 /embeddings 接口，可换硅基流动 / 百炼 / Ollama / 任意兼容端点。

另提供 `HashingEmbedding`：确定性离线实现（分词 → 哈希分桶 → L2 归一化）。
它不是为了替代真模型，而是为了让整条检索管线在**没有 API Key 的环境下可测**——
它给出的相似度是真实的（共享词项越多的文本余弦越高），因此
"query 应把端口那条排到前面"这类断言是有意义的，不是自欺欺人。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol

import httpx

from duramem import net
from duramem.text.tokenize import Segmenter, get_segmenter


class EmbeddingError(RuntimeError):
    pass


def offline_model_name(dim: int) -> str:
    """离线实现的模型标识。

    单独抽成函数是因为它同时是**库内记录的向量来源指纹**——
    `HashingEmbedding`、`describe_embedding_provider` 与一致性校验三处
    必须得到同一个字符串，否则会把"来源没变"误判成"换了模型"。
    """
    return f"offline-hashing-{dim}"


def describe_embedding_provider(
    api_key: str, model: str, dim: int, force_offline: bool = False
) -> tuple[str, bool]:
    """不建连接地算出"实际会用哪个提供方"。

    返回 `(有效模型标识, 是否离线)`。判定条件与 `build_embedding_provider`
    完全一致：勾了离线、或者没有 Key，都会退到哈希实现。
    """
    if force_offline or not api_key:
        return offline_model_name(dim), True
    return model, False


class EmbeddingProvider(Protocol):
    dim: int
    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]: ...


class OpenAICompatibleEmbedding:
    """OpenAI 兼容 /embeddings。自动分批，按 index 重排结果。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        batch_size: int = 32,
        timeout: float = 60.0,
        send_dimensions: bool = False,
    ) -> None:
        if not api_key:
            raise EmbeddingError("缺少 EMBEDDING_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.batch_size = max(1, batch_size)
        # 只有声明支持 `dimensions` 的端点才带这个参数：百炼的
        # text-embedding-v3/v4 靠它选维度（默认 1024），而多传未知字段
        # 有被严格端点 400 拒绝的先例，所以默认不传。
        self.send_dimensions = send_dimensions
        # 只记超时，不建客户端：客户端在首次真正发请求时才取（duramem.net）。
        # 构造期建客户端会让"打开库"这条路径替一份可能用不上的传输付 SSL 上下文
        # 开销——实测每建一个客户端 3.4s，四个提供方合计 12.6s。
        self._timeout = timeout

    def _post_batch(self, batch: Sequence[str]) -> list[list[float]]:
        payload: dict[str, object] = {
            "model": self.model,
            "input": list(batch),
            "encoding_format": "float",
        }
        if self.send_dimensions:
            payload["dimensions"] = self.dim
        try:
            response = net.shared_client().post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # 连接被拒 / 超时 / DNS 失败：归一到 EmbeddingError。
            # 不包这一层的话，调用方拿到的是 httpx.ConnectError——
            # REST 层只认自己人的异常类型，于是界面显示一句
            # "Internal Server Error"，把"网络不通"藏了起来。
            raise EmbeddingError(f"连不上嵌入接口 {self.base_url}：{exc}") from exc
        if response.status_code >= 400:
            raise EmbeddingError(
                f"嵌入接口返回 {response.status_code}：{response.text[:300]}"
            )
        # 响应体用独立名字：上面那个 payload 是请求体（dict[str, object]），
        # 复用同一个名字会让下面的 items 被推断成 object、并在类型上藏住错误。
        body = response.json()
        items = body.get("data") or []
        if len(items) != len(batch):
            raise EmbeddingError(
                f"嵌入条数不符：请求 {len(batch)}，返回 {len(items)}"
            )
        items = sorted(items, key=lambda item: item.get("index", 0))
        vectors = [list(map(float, item["embedding"])) for item in items]
        for vec in vectors:
            if len(vec) != self.dim:
                raise EmbeddingError(
                    f"嵌入维度不符：配置 {self.dim}，实际 {len(vec)}。"
                    f"请校正 EMBEDDING_DIM。"
                )
        return vectors

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            out.extend(self._post_batch(texts[start : start + self.batch_size]))
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class HashingEmbedding:
    """确定性离线嵌入：分词 → 哈希分桶 → 词频加权 → L2 归一化。"""

    def __init__(self, dim: int = 1024, segmenter: Segmenter | None = None) -> None:
        self.dim = dim
        self.model = offline_model_name(dim)
        self._segmenter = segmenter

    @property
    def segmenter(self) -> Segmenter:
        if self._segmenter is None:
            self._segmenter = get_segmenter()
        return self._segmenter

    def _bucket(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        tokens = self.segmenter.tokens(text or "")
        for token in tokens:
            vector[self._bucket(token)] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0.0:
            # 空文本给一个固定单位向量，避免除零
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]

    def embed_one(self, text: str) -> list[float]:
        return self._embed_one(text)


def build_embedding_provider(
    api_key: str,
    base_url: str,
    model: str,
    dim: int,
    batch_size: int,
    force_offline: bool = False,
    segmenter: Segmenter | None = None,
    send_dimensions: bool = False,
) -> EmbeddingProvider:
    if force_offline or not api_key:
        return HashingEmbedding(dim=dim, segmenter=segmenter)
    return OpenAICompatibleEmbedding(
        base_url, api_key, model, dim, batch_size, send_dimensions=send_dimensions
    )


__all__ = [
    "EmbeddingError",
    "EmbeddingProvider",
    "HashingEmbedding",
    "OpenAICompatibleEmbedding",
    "build_embedding_provider",
    "describe_embedding_provider",
    "offline_model_name",
]
