"""分词层测试。

这层存在的唯一理由：实测 jieba 会把 `ERR_CONN_REFUSED_0x7f` 切成
`ERR / _ / CONN / _ / REFUSED / _ / 0x7f`，正好毁掉词法路最该救的那类查询。
下面每条断言都对应一个实测到的问题。
"""

from __future__ import annotations

from duramem.text.tokenize import Segmenter, estimate_tokens


def test_error_code_stays_whole():
    seg = Segmenter()
    tokens = seg.tokens("报错 ERR_CONN_REFUSED_0x7f 需要排查")
    assert "ERR_CONN_REFUSED_0x7f" in tokens


def test_identifier_with_underscore_stays_whole():
    seg = Segmenter()
    assert "BACKEND_PORT" in seg.tokens("把 BACKEND_PORT 改掉")


def test_version_and_hyphen_identifiers_stay_whole():
    seg = Segmenter()
    tokens = seg.tokens("用 bge-reranker-v2-m3 重排，版本 v1.2.3")
    assert "bge-reranker-v2-m3" in tokens
    assert "v1.2.3" in tokens


def test_path_keeps_both_granularity_levels():
    """路径含 `/`、`:`、`.`，jieba 的块切分正则会在词典介入前就切开，
    add_word 对它无效。所以必须同时保留整串与碎片两种粒度。"""
    seg = Segmenter()
    tokens = seg.tokens("配置文件在 E:/work/Duramem/.env")
    assert any("work/Duramem/.env" in token for token in tokens), (
        f"整串路径应保留，否则整路径查询无法命中；实际 {tokens}"
    )
    assert "env" in tokens, "basename 也应保留，否则 'env 文件' 这类查询无法命中"


def test_query_expression_escapes_tokens():
    seg = Segmenter()
    expr = seg.segment_query("ERR_CONN_REFUSED_0x7f 怎么解决")
    assert '"ERR_CONN_REFUSED_0x7f"' in expr
    assert " OR " in expr


def test_stopwords_removed_by_default():
    seg = Segmenter(stopwords_enabled=True)
    assert "的" not in seg.tokens("这是的记忆")


def test_custom_words_accepted():
    seg = Segmenter(extra_words=["Duramem"])
    assert "Duramem" in seg.tokens("Duramem 的记忆")


def test_estimate_tokens_scales_with_cjk():
    assert estimate_tokens("这是一段中文") == 6
    assert estimate_tokens("") == 0
    # 中英混排下同样字符数的 token 数差异巨大——所以 L0 上限必须按 token 而非字符
    assert estimate_tokens("中文中文中文中文") > estimate_tokens("abcdefgh")


def test_search_text_includes_title_and_keywords():
    """search_text 是空格分隔的**词项**，所以断言要按词项看，不能按整句。"""
    seg = Segmenter()
    text = seg.build_search_text("摘要内容", "标题词", ["关键词甲", "关键词乙"])
    tokens = set(text.split())
    assert "摘要" in tokens and "内容" in tokens
    assert "标题" in tokens and "词" in tokens
    assert "关键词" in tokens and "甲" in tokens and "乙" in tokens
