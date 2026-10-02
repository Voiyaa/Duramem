"""A.18 决策一的回归：conftest 夹具的"基线复用 + 快照恢复"必须给出内容级隔离。

锁定的契约（设计文档 A.18 验收第 1 条）：
1. 跨用例隔离——上一用例经正常分词路径注册的动态受保护 token、直接写入的
   原生词频/前缀占位、词性表、以及 HMM 强制切分（del_word 的残留路径），
   一律不得泄漏进下一用例；
2. 恢复后的单例与全新 Segmenter 逐字节一致（FREQ / total / user_word_tag_tab /
   lcut / tokens 双粒度输出）；
3. 生产 `reset_segmenter()` 契约不变：置 None、下次 get 重建新实例且可用。

用例 1/2 是一对：1 故意污染单例，2 验证污染没有跨过夹具边界（pytest 按定义
顺序执行，单独运行 2 时状态本就干净、同样通过）。夹具实现见 conftest.py
的 `_snapshot_segmenter` / `_restore_segmenter`。
"""

from __future__ import annotations

from jieba import finalseg

from duramem.text import tokenize
from duramem.text.tokenize import Segmenter, get_segmenter, reset_segmenter

# 覆盖受保护 token、路径双粒度（A.3）、版本号、纯中文 HMM 路径；
# "强制切分"一词若残留在 Force_Split_Words，lcut 会把它逐字硬拆，逐字节断言即失败。
_ISOLATION_TEXTS = [
    "报错 ERR_CONN_REFUSED_0x7f 需要排查",
    "配置文件在 E:/work/Duramem/.env 里",
    "用 bge-reranker-v2-m3 重排，版本 v1.2.3",
    "把 BACKEND_PORT 改掉，默认 8001",
    "这里强制切分四个字必须按词典切，不该被逐字硬拆",
    "netstat -ano | findstr 8001",
    "中文分词的普通文本，走 HMM 路径",
]


def test_previous_case_drifts_segmenter_state():
    """模拟"上一个用例"：先断言四类漂移确实发生，否则用例 2 的隔离断言是空转。"""
    seg = get_segmenter()
    seg.tokens("报错 ERR_CONN_REFUSED_0x7f 在 E:/work/Duramem/.env")  # 正常路径注册动态 token
    seg._tk.add_word("隔离测试原生词", 123)  # FREQ + total + 前缀占位（jieba add_word:429-435）
    seg._tk.add_word("隔离测试标签词", 5, "n")  # user_word_tag_tab
    finalseg.add_force_split("强制切分")  # del_word → add_word(freq=0) 的残留路径

    assert "ERR_CONN_REFUSED_0x7f" in seg._dynamic
    assert "隔离测试原生词" in seg._tk.FREQ
    assert "隔离测试标签词" in seg._tk.user_word_tag_tab
    assert "强制切分" in finalseg.Force_Split_Words


def test_next_case_sees_fresh_dictionary():
    """上一用例的漂移不得泄漏：恢复后的单例与全新实例全量状态一致、输出逐字节一致。"""
    fresh = Segmenter()
    fresh._tk.initialize()  # jieba 懒加载：显式触发词典构建
    seg = get_segmenter()

    assert seg._dynamic == set(), "动态受保护 token 泄漏"
    assert seg._tk.FREQ == fresh._tk.FREQ, "词频/前缀占位泄漏"
    assert seg._tk.total == fresh._tk.total, "total 泄漏（归一化基准被改变）"
    assert seg._tk.user_word_tag_tab == fresh._tk.user_word_tag_tab, "词性表泄漏"
    assert finalseg.Force_Split_Words.isdisjoint(
        {"ERR_CONN_REFUSED_0x7f", "E:/work/Duramem/.env", "强制切分"}
    ), "HMM 强制切分泄漏"

    for text in _ISOLATION_TEXTS:
        assert seg._tk.lcut(text, HMM=True) == fresh._tk.lcut(text, HMM=True), text
        assert seg.tokens(text) == fresh.tokens(text), text


def test_reset_segmenter_keeps_production_contract():
    """生产 reset_segmenter() 语义必须保持：置 None、下次 get 重建新实例且可用。
    本用例付一次真实的词典重建（~0.6s），把这条契约钉在套件里。"""
    old = get_segmenter()
    reset_segmenter()
    assert tokenize._segmenter is None

    rebuilt = get_segmenter()
    assert rebuilt is not old
    assert "ERR_CONN_REFUSED_0x7f" in rebuilt.tokens("报错 ERR_CONN_REFUSED_0x7f")
