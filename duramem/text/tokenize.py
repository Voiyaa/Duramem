"""中文分词与受保护 token 处理。

为什么需要这一层：实测 jieba 会把 `ERR_CONN_REFUSED_0x7f` 切成
`ERR / _ / CONN / _ / REFUSED / _ / 0x7f` —— 正是词法路本该救回来的那类查询。
所以先识别"必须整体保留"的 token（错误码、标识符、路径、版本号），
用 add_word 灌进 jieba，再做分词。
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import jieba

# jieba 默认会把"Building prefix dict..."打到 stderr，污染 CLI 与 MCP 的输出流。
# MCP 的 stdio 传输尤其敏感：stdout 是协议通道，任何杂音都可能被当成协议帧。
jieba.setLogLevel(logging.ERROR)

# 必须整体保留的 token：
#   1) 十六进制字面量          0x7f
#   2) 含分隔符的标识符        err_conn / a.b.c / foo-bar / path/to
#   3) 全大写代号              ERR / HTTP / API
#   4) 版本号                  1.2.3
_PROTECTED_RE = re.compile(
    r"0[xX][0-9a-fA-F]+"
    # 分隔符允许连续出现：`/.env` 里 `/` 和 `.` 是连着的，
    # 若只允许单个分隔符就只能匹配到 `work/Duramem` 而丢掉 `/.env`
    r"|[A-Za-z0-9]+(?:[_.\-/:]+[A-Za-z0-9]+)+"
    r"|[A-Z]{2,}[0-9]*"
    r"|\d+\.\d+(?:\.\d+)*"
)

# 中文功能词。BM25 里这些几乎只贡献噪声。
_STOPWORDS = frozenset(
    """
    的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好
    自己 这 那 他 她 它 们 而 及 与 或 但 又 把 被 让 从 对 向 于 为 以 之 其 此 该
    呢 吗 吧 啊 呀 哦 嗯 哈 么 嘛 啦 咯 唉 哎 咦 哇 哟 嘿
    个 些 这 那 里 外 中 内 前 后 左 右 时 候 次 种 点 面 边 头 间
    a an the is are was were be been being of to in on at by for with and or
    """.split()
)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    中日韩字符按 1:1 计，ASCII 字母数字按 4 字符 1 token 计，其余按 2 字符 1 token。
    仅用于 L0 上限与回查预算的软约束，不与任何具体 tokenizer 对齐。
    """
    if not text:
        return 0
    cjk = 0
    ascii_alnum = 0
    other = 0
    for ch in text:
        code = ord(ch)
        if 0x3400 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF:
            cjk += 1
        elif ch.isascii() and (ch.isalnum()):
            ascii_alnum += 1
        else:
            other += 1
    return cjk + -(-ascii_alnum // 4) + -(-other // 2)


class Segmenter:
    """jieba 分词器封装。

    使用独立的 `jieba.Tokenizer()` 实例，避免污染全局词典。
    add_word 有数量上限，防止长时间运行后词典无界增长。
    """

    MAX_DYNAMIC_WORDS = 20_000

    def __init__(
        self,
        extra_words: list[str] | None = None,
        user_dict: str | Path | None = None,
        stopwords_enabled: bool = True,
    ) -> None:
        self._tk = jieba.Tokenizer()
        self._lock = threading.Lock()
        self._dynamic: set[str] = set()
        self._stopwords = _STOPWORDS if stopwords_enabled else frozenset()

        if user_dict:
            path = Path(user_dict)
            if path.exists():
                self._tk.load_userdict(str(path))

        for word in extra_words or []:
            self._register(word)

    # ------------------------------------------------------------------

    def _register(self, word: str) -> None:
        """把受保护 token 注册为整体词。"""
        word = word.strip()
        if not word or word in self._dynamic:
            return
        if len(self._dynamic) >= self.MAX_DYNAMIC_WORDS:
            return
        with self._lock:
            if word in self._dynamic:
                return
            self._tk.add_word(word, freq=10_000_000)
            self._dynamic.add(word)

    def _protect(self, text: str) -> None:
        for match in _PROTECTED_RE.finditer(text):
            self._register(match.group(0))

    # ------------------------------------------------------------------

    def tokens(self, text: str) -> list[str]:
        """切词并去停用词。保留原始大小写（FTS5 unicode61 会做 ASCII 折叠）。

        受保护 token 会**额外**作为独立词项追加。原因：jieba 内部的块切分正则
        在词典介入之前就按 `/`、`:` 切开了，add_word 对含这些分隔符的串无效
        （实测 `E:/a/b/.env` 会被切成 `E a b env`）。同时保留整串与碎片两种粒度，
        让整路径查询和 basename 查询都能命中。
        """
        if not text:
            return []
        self._protect(text)
        protected = [m.group(0) for m in _PROTECTED_RE.finditer(text)]

        out: list[str] = []
        seen: set[str] = set()
        for token in self._tk.lcut(text, HMM=True):
            token = token.strip()
            if not token or token in seen:
                continue
            if token in self._stopwords:
                continue
            if not any(ch.isalnum() for ch in token):
                # 纯标点/空白，丢弃
                continue
            seen.add(token)
            out.append(token)

        for token in protected:
            if token in seen or token in self._stopwords:
                continue
            seen.add(token)
            out.append(token)

        return out

    def segment(self, text: str) -> str:
        """产出供 FTS5 索引的空格分隔串。"""
        return " ".join(self.tokens(text))

    def segment_query(self, text: str) -> str:
        """产出 FTS5 MATCH 表达式。

        用 OR 连接，让任意一个 token 命中即召回；BM25 会按命中词加权排序。
        """
        toks = self.tokens(text)
        if not toks:
            return ""
        escaped = [f'"{t}"' for t in toks]
        return " OR ".join(escaped)

    def build_search_text(
        self, summary_text: str, title: str = "", keywords: list[str] | None = None
    ) -> str:
        """索引文本 = L0 + 标题 + 关键词，统一分词。"""
        parts = [summary_text or "", title or "", " ".join(keywords or [])]
        return self.segment(" ".join(parts))


_segmenter: Segmenter | None = None
_segmenter_lock = threading.Lock()


def get_segmenter(
    extra_words: list[str] | None = None,
    user_dict: str | Path | None = None,
    stopwords_enabled: bool = True,
) -> Segmenter:
    global _segmenter
    if _segmenter is None:
        with _segmenter_lock:
            if _segmenter is None:
                _segmenter = Segmenter(extra_words, user_dict, stopwords_enabled)
    return _segmenter


def reset_segmenter() -> None:
    """测试用。"""
    global _segmenter
    _segmenter = None
