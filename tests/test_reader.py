"""原文回查（L2）测试：mode 粒度、detail 档位、取回量计数。

取回量这条经历过一次方向反转，这里记录清楚以免被改回去：
v1 写的"回查限制无"曾被改成**每轮硬限额**，理由是会把数万 token 灌进宿主。
该设计已撤销，改照 OpenViking——它对 L2 读取不设上限，体量靠定位精度控制。
所以现在计数只用于**报告与提示**，任何情况下都不拒绝回查。
"""

from __future__ import annotations

import pytest
from conftest import WINDOW

from duramem.reader import ReadUsageTracker, truncate_to_tokens


@pytest.fixture
def uid(seeded) -> str:
    res = seeded.search("端口", db="work")
    assert res.hits
    return res.hits[0].uid


# ============================================================== 取回量计数


def test_usage_tracker_counts_and_resets():
    tracker = ReadUsageTracker(soft_limit_tokens=100, idle_reset_seconds=3600)
    tracker.begin_round("db", "s")
    assert tracker.used("db", "s") == 0

    assert tracker.record("db", "s", 60) == 60
    # 累计而不是覆盖，且**不因超额而拒绝**——这是与旧限额器的根本区别
    assert tracker.record("db", "s", 50) == 110
    assert tracker.used("db", "s") == 110

    tracker.begin_round("db", "s")
    assert tracker.used("db", "s") == 0


def test_usage_is_per_session():
    tracker = ReadUsageTracker(soft_limit_tokens=50, idle_reset_seconds=3600)
    tracker.begin_round("db", "a")
    tracker.record("db", "a", 50)
    assert tracker.used("db", "a") == 50
    assert tracker.used("db", "b") == 0, "不同会话不应共享计数"


def test_usage_expires_after_idle():
    tracker = ReadUsageTracker(soft_limit_tokens=10, idle_reset_seconds=0)
    tracker.begin_round("db", "s")
    tracker.record("db", "s", 10)
    assert tracker.used("db", "s") == 0, "空闲超时后计数应自动归零"


def test_soft_limit_is_advisory_only():
    """软阈值只影响 over_soft_limit 这个判断，不影响 record 的结果。"""
    tracker = ReadUsageTracker(soft_limit_tokens=100, idle_reset_seconds=3600)
    tracker.begin_round("db", "s")
    tracker.record("db", "s", 5000)
    assert tracker.over_soft_limit("db", "s") is True
    assert tracker.used("db", "s") == 5000, "超阈值也要如实记账"


def test_no_soft_limit_by_default():
    """默认 0 = 不设限，也就是完全照 OpenViking：读多少给多少。"""
    tracker = ReadUsageTracker()
    assert tracker.soft_limit_tokens == 0
    tracker.begin_round("db", "s")
    tracker.record("db", "s", 999_999)
    assert tracker.over_soft_limit("db", "s") is False


def test_truncate_to_tokens():
    text = "中文内容" * 200
    truncated, was_cut = truncate_to_tokens(text, 50)
    assert was_cut
    assert len(truncated) < len(text)

    same, was_cut = truncate_to_tokens("短文本", 100)
    assert same == "短文本" and not was_cut

    assert truncate_to_tokens("任何内容", 0) == ("", True)


def test_truncate_prefers_sentence_end():
    """截断该落在句末，不该把词切开。

    实测过真实伤害：按字符二分会把 `parse-transcript` 切成 `parse-transcri`，
    读者分不清那是原文的样子还是被切过。
    """
    text = "第一句话结束了。第二句话也结束了。第三句话在这里被截断掉一半多"
    truncated, was_cut = truncate_to_tokens(text, 12)
    assert was_cut
    assert truncated.endswith("。"), f"应截在句末，实际切成了 {truncated!r}"


def test_truncate_does_not_break_on_decimal_point():
    """小数点不是句末：`x 1.5` 不该被切成 `x 1.`"""
    text = "误差收敛到 1.5 秒以内然后后面还有很多很多内容继续写下去不停"
    truncated, was_cut = truncate_to_tokens(text, 8)
    assert was_cut
    if truncated.endswith("."):
        raise AssertionError(f"切在了小数点上：{truncated!r}")


def test_truncate_falls_back_when_no_sentence_end():
    """一整段没有句末时，退回原始切点而不是退到开头（那等于什么都没给）。"""
    text = "没有任何标点的长文本" * 50
    truncated, was_cut = truncate_to_tokens(text, 20)
    assert was_cut
    assert len(truncated) > 10, "不该退到开头"


# ====================================================================== mode


def test_quote_returns_exact_span(seeded, uid):
    result = seeded.read_original(uid, db="work", mode="quote")
    assert result.ok
    chunk = seeded._components("work").repo.get_chunk(uid)
    assert result.text == chunk["original_text"][chunk["original_char_start"] : chunk["original_char_end"]]


def test_window_expands_by_neighbouring_messages(seeded, uid):
    quote = seeded.read_original(uid, db="work", mode="quote")
    window = seeded.read_original(uid, db="work", mode="window", window=1)
    assert window.ok
    assert len(window.text) >= len(quote.text), "window 应包含 quote 的前提/后续"

    wider = seeded.read_original(uid, db="work", mode="window", window=3)
    assert len(wider.text) >= len(window.text)


def test_full_returns_whole_l1(seeded, uid):
    result = seeded.read_original(uid, db="work", mode="full")
    chunk = seeded._components("work").repo.get_chunk(uid)
    assert result.text == chunk["original_text"]


def test_window_does_not_leak_across_sessions(seeded, uid):
    """window 扩展必须限制在切片自己的会话里。

    `messages.id` 是全库自增的，不限制会话时"前后各 N 条"会取回**别的对话**的
    消息，而返回体里仍标着本切片的 msg_range——原文被张冠李戴，模型无从察觉。
    """
    from conftest import DIALOGUE, WINDOW

    from duramem.models import Message

    other = [
        Message(WINDOW, "chat-B", index, role, f"别的会话内容 标记ZZZ {content}")
        for index, (role, content) in enumerate(DIALOGUE[:4])
    ]
    seeded.ingest(other, db="work")

    leaked = seeded.read_original(uid, db="work", mode="window", window=3, detail="full")
    assert leaked.ok
    assert "标记ZZZ" not in leaked.text, "window 不应扩到别的会话去"


def test_window_expansion_can_be_empty_at_session_edge(seeded, uid):
    """切片已经在会话边缘时，没有可扩的邻居就是没有——不该从别处补。"""
    from duramem.models import Message

    # 另一个会话的消息 id 紧邻本会话，是最容易漏进来的位置
    seeded.ingest(
        [Message(WINDOW, "chat-C", 0, "user", "边缘另一侧 标记YYY")], db="work"
    )
    out = seeded.read_original(uid, db="work", mode="window", window=1, detail="full")
    assert out.ok
    assert "标记YYY" not in out.text


def test_session_summary_without_range_falls_back_to_full(seeded):
    """会话级总结没有消息区间，回查时不应报错，而是退回 l1 快照。"""
    from duramem.models import ChunkDraft

    repo = seeded._components("work").repo
    result = repo.insert_chunk(
        ChunkDraft(summary_text="会话级总结", original_text="总结全文内容", tags=["session_summary"])
    )
    out = seeded.read_original(result["uid"], db="work", mode="window")
    assert out.ok
    assert out.text == "总结全文内容"
    assert any("消息区间" in w for w in out.warnings)


# ====================================================================== detail


def test_minimal_truncates(seeded, uid):
    full = seeded.read_original(uid, db="work", mode="full", detail="full")
    minimal = seeded.read_original(uid, db="work", mode="full", detail="minimal")
    assert minimal.ok
    if full.tokens > 500:
        assert minimal.tokens < full.tokens
        assert minimal.truncated


def test_invalid_mode_is_rejected(seeded, uid):
    result = seeded.read_original(uid, db="work", mode="nonsense")
    assert not result.ok
    assert any("不支持的 mode" in w for w in result.warnings)


def test_invalid_detail_is_rejected(seeded, uid):
    result = seeded.read_original(uid, db="work", detail="nonsense")
    assert not result.ok
    assert any("不支持的 detail" in w for w in result.warnings)


def test_unknown_uid_returns_error_not_exception(seeded):
    result = seeded.read_original("nonexistent-uid", db="work")
    assert not result.ok
    assert result.text == ""
    assert any("不存在" in w for w in result.warnings)


# ============================================================== 取回量生效


def test_repeated_reads_are_never_refused(seeded, uid):
    """反复回查不被拒绝——这是撤销硬限额后最该守住的行为。

    旧设计的实测伤害：一条 7956 token 的区间被拒，退化成 500 token 的摘要，
    丢 94%。现在读多少给多少，只如实记账。
    """
    probe = seeded.read_original(uid, db="work", mode="window", window=3, detail="full")
    assert probe.ok and probe.tokens > 0

    # 把软阈值设成远小于一次读取，确认它只提示不拦截
    seeded.settings.read_soft_limit_tokens = 1
    seeded.usage.soft_limit_tokens = 1
    seeded.usage.begin_round("work", "default")

    for _ in range(3):
        again = seeded.read_original(uid, db="work", mode="window", window=3, detail="full")
        assert again.ok, "无论累计多少，回查都该成功"
        assert again.truncated is False, "不该被截断"

    # 超阈值只加一句提示，且如实报告累计量
    assert again.tokens_this_round >= probe.tokens * 3
    assert any("软阈值" in w for w in again.warnings)
    assert any("不影响本次返回" in w for w in again.warnings)


def test_no_soft_limit_means_no_warning(seeded, uid):
    """默认不设阈值时连提示都不该出现。"""
    seeded.usage.begin_round("work", "default")
    for _ in range(3):
        result = seeded.read_original(uid, db="work", mode="full", detail="full")
        assert result.ok
    assert not any("软阈值" in w for w in result.warnings)
    assert result.tokens_this_round > 0


def test_long_range_that_used_to_be_refused_now_succeeds(seeded):
    """回归：跨多条消息的 window 读取曾因超预算被拒，现在必须成功。

    这条对应真实遇到的失败——window=2 展开后需要 7956 token，而旧限额是
    4000，于是被拒并退回摘要。
    """
    hits = seeded.search("端口", db="work").hits
    assert hits
    result = seeded.read_original(
        hits[0].uid, db="work", mode="window", window=3, detail="full"
    )
    assert result.ok
    assert result.text
    assert result.truncated is False


def test_search_resets_read_usage(seeded, uid):
    seeded.read_original(uid, db="work", mode="full", detail="full")
    assert seeded.usage.used("work", "default") > 0

    seeded.search("端口", db="work")
    assert seeded.usage.used("work", "default") == 0, "新一轮检索应重置取回计数"


# ====================================================================== 开关


def test_l2_access_can_be_disabled(seeded, uid):
    """L2 开关：关掉后原文不可回查。"""
    seeded.settings.original_access_enabled = False
    result = seeded.read_original(uid, db="work", mode="full")
    assert not result.ok
    assert result.text == ""
    assert any("已关闭 L2 原文回查" in w for w in result.warnings)


def test_disabled_l2_marks_hits_as_no_original(seeded):
    seeded.settings.original_access_enabled = False
    res = seeded.search("端口", db="work")
    assert res.hits
    assert all(hit.has_original is False for hit in res.hits), (
        "关闭 L1 后 has_original 必须为 false，否则模型会去调一个必然失败的工具"
    )


# ====================================================================== 相邻


def test_read_neighbors_marks_current(seeded):
    res = seeded.search("端口", db="work")
    assert res.hits
    uid = res.hits[0].uid
    payload = seeded.read_neighbors(uid, db="work", before=1, after=1)
    assert payload["ok"]
    assert payload["count"] >= 1
    currents = [n for n in payload["neighbors"] if n["current"]]
    assert len(currents) == 1 and currents[0]["uid"] == uid


def test_neighbors_excludes_deleted(seeded):
    res = seeded.search("端口", db="work")
    uid = res.hits[0].uid
    before = seeded.read_neighbors(uid, db="work", before=5, after=5)["neighbors"]
    if len(before) > 1:
        victim = next(n for n in before if not n["current"])
        seeded.forget(victim["uid"])
        after = seeded.read_neighbors(uid, db="work", before=5, after=5)["neighbors"]
        assert victim["uid"] not in [n["uid"] for n in after]


# ====================================================================== 建议


def test_hits_carry_actionable_suggestion(seeded):
    """R5 要靠这个信号成立：不给依据，模型不会知道摘要够不够用。"""
    res = seeded.search("端口", db="work")
    with_original = [h for h in res.hits if h.has_original]
    assert with_original
    for hit in with_original:
        assert hit.suggestion
        assert "dm_read_original" in hit.suggestion
    assert res.suggestion and "dm_read_original" in res.suggestion


def test_mcp_payload_exposes_decision_fields(seeded):
    payload = seeded.search("端口", db="work").to_mcp_payload()
    assert payload["retrieval_mode"] == "hybrid"
    hit = payload["results"][0]
    for field in ("uid", "title", "summary", "score", "hit_count", "has_original"):
        assert field in hit
    assert "vec_rank" not in hit, "内部排名不应暴露给模型，避免它按排名做无谓判断"
