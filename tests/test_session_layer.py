"""会话层（容器层）测试：L0 机械派生、抽样、落库、端到端生成。

这里最该守住的两条不变式：

1. **L0 是从 L1 派生的，不是第二次模型调用**。所以两者永不互相矛盾，也没有
   额外生成成本。抽不出正文时必须退回 L1 开头而不是返回空——空 L0 会让这个
   会话在词法检索里彻底消失。
2. **导航段里的 uid 必须真实存在**。L1 相对 L0 的核心增量就是"该去读哪一条"，
   uid 编造的话整个渐进式披露链就断了。
"""

from __future__ import annotations

import pytest
from conftest import SESSION, WINDOW, dialogue_messages

from duramem.session_summarizer import (
    HeuristicOverviewProvider,
    abstract_from_overview,
    sample_slices,
)
from duramem.store.session_layers import (
    SessionLayerDraft,
    SessionLayerStore,
    overview_digest,
)
from duramem.text.tokenize import get_segmenter

OVERVIEW = """---
generated_by: test
---

# 会话 chat-A

这段会话从后端连接被拒开始，定位到端口占用，改端口后解决；随后讨论许可证与向量库选型。

## 覆盖度

基于 4 条切片，覆盖整个会话，未抽样。

## 导航

- 想知道端口占用怎么查 → uid: aaa111bbb222（结论：netstat 找 PID 再 taskkill）
- 想知道许可证的坑 → uid: ccc333ddd444（结论：避开 AGPL 与 CC-BY-NC）
"""


# ============================================================== L0 派生


def test_l0_takes_the_paragraph_before_first_h2():
    l0 = abstract_from_overview(OVERVIEW, 256)
    assert l0.startswith("这段会话从后端连接被拒开始")
    # 二级标题之后的内容不该进 L0
    assert "覆盖度" not in l0
    assert "uid:" not in l0
    # 一级标题与 frontmatter 也不该进
    assert not l0.startswith("#")
    assert "generated_by" not in l0


def test_l0_is_truncated_at_a_break():
    """截断落在标点处，不切在词中间。

    中文长句常常只用一个逗号，所以"句末"找不到时要退到子句分隔符——
    实测过切在「定位到端口占」这种半个词上。
    """
    full = "这段会话从后端连接被拒开始，定位到端口占用，改端口后解决；随后讨论许可证与向量库选型。"
    l0 = abstract_from_overview(OVERVIEW, 20)
    assert len(l0) < len(full), "应被截断"
    assert l0[-1] in "。！？；，、：", f"应在标点处收住：{l0!r}"


def test_l0_never_empty_when_there_is_no_paragraph():
    """没有正文段落时退回整段开头——绝不能返回空。

    L0 是会话进词法索引的唯一来源，空 L0 等于这个会话在关键词检索里不存在。
    """
    only_headings = "# 标题\n\n## 覆盖度\n\n基于 3 条切片。\n"
    l0 = abstract_from_overview(only_headings, 256)
    assert l0.strip(), "不能返回空"
    assert "基于 3 条切片" in l0


def test_l0_handles_plain_text_without_structure():
    l0 = abstract_from_overview("就是一段没有标题的普通文本。第二句在这里。", 256)
    assert l0.startswith("就是一段没有标题的普通文本。")


# ============================================================== 抽样


def _rows(n: int) -> list[dict]:
    return [{"chunk_uid": f"uid{i:02d}"} for i in range(n)]


def test_sample_keeps_head_and_tail():
    """首尾必须都在：尾部是最近的结论，导航最该指向它。"""
    picked = sample_slices(_rows(10), 4)
    assert len(picked) == 4
    assert picked[0]["chunk_uid"] == "uid00"
    assert picked[-1]["chunk_uid"] == "uid09"


def test_sample_near_the_limit_still_keeps_the_tail():
    """回归：旧的 int(i*step) 公式在 limit 接近总数时会丢掉最后一条。"""
    picked = sample_slices(_rows(10), 9)
    assert picked[-1]["chunk_uid"] == "uid09"
    assert len({r["chunk_uid"] for r in picked}) == len(picked), "不该重复取同一条"


def test_sample_is_noop_when_under_limit():
    rows = _rows(3)
    assert sample_slices(rows, 32) == rows
    assert sample_slices(rows, 0) == rows, "0 表示不设限"


def test_sample_of_one_returns_the_latest():
    assert sample_slices(_rows(5), 1)[0]["chunk_uid"] == "uid04"


# ============================================================== 指纹


def test_digest_ignores_whitespace_but_not_case():
    """空白差异不算内容变化；大小写差异**算**。

    与去重用的 normalize_for_hash 刻意不同：那个折叠大小写是对的（同一条记忆），
    这里折叠会把真实改动判成"没变"，于是概览永远不刷新。
    """
    assert overview_digest("正文  内容\n第二行") == overview_digest("正文 内容 第二行")
    assert overview_digest("Hello") != overview_digest("hello")


# ============================================================== 落库


@pytest.fixture
def store(service) -> SessionLayerStore:
    return SessionLayerStore(service._components("work").db, get_segmenter())


def test_upsert_creates_then_updates_and_clears_pending(store):
    draft = SessionLayerDraft(
        window_id=WINDOW,
        session_id=SESSION,
        overview_text=OVERVIEW,
        abstract_text=abstract_from_overview(OVERVIEW, 256),
        coverage_total=4,
        coverage_sampled=4,
        model_used="test",
    )
    row = store.upsert(draft)
    assert row["overview_digest"] == overview_digest(OVERVIEW)
    assert row["abstract_tokens"] > 0 and row["overview_tokens"] > 0

    # 记两次待刷新，再生成一次应该清零
    with store.db.write() as conn:
        from duramem.store.session_layers import bump_pending

        bump_pending(conn, WINDOW, SESSION)
        bump_pending(conn, WINDOW, SESSION)
    assert store.pending(WINDOW, SESSION) == 2

    store.upsert(
        SessionLayerDraft(
            window_id=WINDOW, session_id=SESSION, overview_text=OVERVIEW + "\n补一段。"
        )
    )
    assert store.pending(WINDOW, SESSION) == 0, "成功生成应消费掉此前的变化计数"
    assert store.count() == 1, "同一个会话只该有一行"


def test_bump_pending_without_a_row_is_a_noop(service):
    """没有概览就没什么可陈旧的，不该凭空造出一行。"""
    components = service._components("work")
    with components.db.write() as conn:
        from duramem.store.session_layers import bump_pending

        bump_pending(conn, "win-unknown", "sess-unknown")
    assert components.session_layers.count() == 0


def test_get_by_session_finds_it_without_window_id(store):
    """模型只有 session_id，没有 window——这条路径必须能用。"""
    store.upsert(
        SessionLayerDraft(window_id=WINDOW, session_id=SESSION, overview_text=OVERVIEW)
    )
    row = store.get_by_session(SESSION)
    assert row is not None
    assert row["session_id"] == SESSION
    assert store.get_by_session("不存在的会话") is None


# ============================================================== 端到端


def test_summarize_session_end_to_end(seeded):
    outcome = seeded.summarize_session(WINDOW, SESSION, db="work")
    assert outcome.ok, outcome.warnings
    assert outcome.coverage_total > 0
    assert outcome.abstract_tokens > 0 and outcome.overview_tokens > 0
    assert outcome.model_used == "offline-heuristic"

    row = seeded.get_session_layer(SESSION, db="work")
    assert row is not None
    l1 = row["overview_text"]
    # 地图四节齐备；详述节必须不存在——L1 由切片 L0 聚合生成，
    # 详述 ⊆ L0，是派生冗余（设计文档 A.16）
    for section in ("## 覆盖度", "## 导航"):
        assert section in l1, f"缺 {section}"
    assert "## 详细说明" not in l1, "详述节不该再出现在 L1 里"
    assert l1.lstrip().startswith("# 会话")


def test_navigation_uids_are_real(seeded):
    """导航段里的 uid 必须真能取到原文——否则渐进式披露链是断的。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    l1 = seeded.get_session_layer(SESSION, db="work")["overview_text"]

    import re

    uids = re.findall(r"uid:\s*([0-9A-Za-z]+)", l1)
    assert uids, "导航段必须给出 uid"
    for uid in uids:
        read = seeded.read_original(uid, db="work", mode="full", detail="full")
        assert read.ok, f"导航指向的 {uid} 取不到原文"
        assert read.text.strip()


def test_abstract_is_derived_from_overview(seeded):
    """L0 必须是 L1 的派生，不是另一次生成——两者不能互相矛盾。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    row = seeded.get_session_layer(SESSION, db="work")
    assert row["abstract_text"]
    assert row["abstract_text"] in row["overview_text"], "简介应是概览里的一段"


def test_summarize_session_without_slices_reports_clearly(seeded):
    outcome = seeded.summarize_session("win-empty", "sess-empty", db="work")
    assert outcome.ok is False
    assert any("还没有切片" in w for w in outcome.warnings)
    assert seeded.get_session_layer("sess-empty", db="work") is None


def test_generation_is_idempotent_on_content(seeded):
    """同样输入生成两次，正文指纹应一致——否则会被判成"一直有变化"。"""
    first = seeded.summarize_session(WINDOW, SESSION, db="work")
    d1 = seeded.get_session_layer(SESSION, db="work")["overview_digest"]
    second = seeded.summarize_session(WINDOW, SESSION, db="work")
    d2 = seeded.get_session_layer(SESSION, db="work")["overview_digest"]
    assert first.ok and second.ok
    assert d1 == d2, "确定性提供方下，同样输入应产出同样的正文"


def test_provider_shape_is_shared_by_both_implementations(settings):
    """两个提供方共用同一个签名，免得调用方要分派。"""
    from duramem.session_summarizer import build_overview_provider

    provider = build_overview_provider(settings, get_segmenter())
    assert isinstance(provider, HeuristicOverviewProvider)
    out = provider.overview(
        [{"chunk_uid": "u1", "title": "标题", "summary_text": "摘要内容"}], "sess", settings
    )
    assert "uid: u1" in out
    assert "## 导航" in out

# ============================================================== 刷新策略


def test_refresh_decision_table():
    """决策表逐行覆盖。参数：has_layer / slice_count / pending / limit=32 / ratio=0.10"""
    from duramem.session_summarizer import (
        MARK_PENDING,
        NOOP,
        REFRESH_NOW,
        decide_refresh,
    )

    def decide(**kw):
        base = dict(
            has_layer=True, slice_count=5, pending=1, sample_limit=32, refresh_ratio=0.10
        )
        base.update(kw)
        return decide_refresh(**base)[0]

    assert decide(pending=0) == NOOP, "没有待跟上变化"
    assert decide(has_layer=False, pending=3) == REFRESH_NOW, "还没有概览"
    # 回归：没概览的会话必然没有 pending 计数，所以这条必须排在 pending 判断之前，
    # 否则首次生成永远走不到（实测踩过）
    assert decide(has_layer=False, pending=0) == REFRESH_NOW, "首次生成不能被 pending 挡住"
    assert decide(slice_count=30, pending=1) == REFRESH_NOW, "小容器立即刷新"
    assert decide(slice_count=100, pending=10) == REFRESH_NOW, "占比达阈值"
    assert decide(slice_count=100, pending=9) == MARK_PENDING, "占比未达阈值"
    # 不限抽样时"小容器"那条不适用，一切由比例定
    assert decide(sample_limit=0, slice_count=1, pending=1) == REFRESH_NOW


def test_refresh_decision_explains_itself():
    """必须能解释"为什么没刷新"，否则用户无从判断是策略拦截还是故障。"""
    from duramem.session_summarizer import decide_refresh

    decision, reason = decide_refresh(
        has_layer=True, slice_count=100, pending=1, sample_limit=32, refresh_ratio=0.10
    )
    assert "1/100" in reason and "10%" in reason

    _, reason = decide_refresh(
        has_layer=True, slice_count=5, pending=2, sample_limit=32, refresh_ratio=0.10
    )
    assert "32" in reason, "该说明为什么小容器不按比例算"


def test_maybe_refresh_generates_when_no_layer_yet(seeded):
    judged = seeded._components("work").session_summarizer.maybe_refresh(WINDOW, SESSION)
    from duramem.session_summarizer import REFRESH_NOW

    assert judged.decision == REFRESH_NOW
    assert judged.summary is not None and judged.summary.ok


def test_maybe_refresh_is_noop_when_nothing_changed(seeded):
    seeded.summarize_session(WINDOW, SESSION, db="work")
    judged = seeded._components("work").session_summarizer.maybe_refresh(WINDOW, SESSION)
    from duramem.session_summarizer import NOOP

    assert judged.decision == NOOP
    assert judged.summary is None, "没生成就该是 None——不能拿 ok=False 表达'策略说先别动'"


# ========================================================== 待刷新计数不变式


def test_new_chunk_marks_the_session_stale(seeded):
    """新切片的 L0 是会话概览的新输入。"""
    from duramem.models import ChunkDraft

    seeded.summarize_session(WINDOW, SESSION, db="work")
    comps = seeded._components("work")
    assert comps.session_layers.pending(WINDOW, SESSION) == 0

    comps.repo.insert_chunk(
        ChunkDraft(summary_text="又想起一件事", source_window=WINDOW, source_session=SESSION)
    )
    assert comps.session_layers.pending(WINDOW, SESSION) == 1


def test_l0_edit_marks_stale_but_identical_rewrite_does_not(seeded):
    """写回同样的 L0 不该记一笔——否则反复保存会把计数推过阈值、白跑一次模型。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    comps = seeded._components("work")
    uid = seeded.search("端口", db="work").hits[0].uid
    original = comps.repo.get_chunk(uid)["summary_text"]

    comps.repo.update_chunk(uid, summary_text=original)
    assert comps.session_layers.pending(WINDOW, SESSION) == 0, "内容没变就不算变化"

    comps.repo.update_chunk(uid, summary_text=original + "（补充）")
    assert comps.session_layers.pending(WINDOW, SESSION) == 1, "真变了才记"


def test_l2_edit_does_not_mark_stale(seeded):
    """L2 不是概览的输入——概览从不读原文。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    comps = seeded._components("work")
    uid = seeded.search("端口", db="work").hits[0].uid

    comps.repo.update_chunk(uid, original_text="完全不同的原文")
    assert comps.session_layers.pending(WINDOW, SESSION) == 0


def test_message_edit_does_not_mark_stale(seeded):
    """消息编辑让 L2 过时，**不让会话概览过时**。

    会话概览的输入是切片 L0，而消息编辑刻意不改 L0（摘要是模型当时的判断，
    见 `refresh_chunks_for_messages` 的说明）。搞反了会让每次编辑都触发一轮
    昂贵的概览重生成。
    """
    from duramem.models import Message

    seeded.summarize_session(WINDOW, SESSION, db="work")
    comps = seeded._components("work")
    assert comps.session_layers.pending(WINDOW, SESSION) == 0

    edited = dialogue_messages()
    edited[1] = Message(WINDOW, SESSION, 1, "assistant", "连接被拒绝，先看端口是否被占")
    seeded.ingest(edited, db="work", auto_summarize=False)

    assert comps.session_layers.pending(WINDOW, SESSION) == 0
    # 但 L2 确实被重渲染了——这条路径不能因为"不记 pending"而失效
    chunk = comps.repo.get_chunk(edited and seeded.search("端口", db="work").hits[0].uid)
    assert "先看端口是否被占" in chunk["original_text"]


def test_manual_store_without_session_marks_nothing(seeded):
    """手动写入的切片不属于任何会话，没有概览会因它陈旧。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    comps = seeded._components("work")
    seeded.store(summary_text="手动记一条", db="work")
    assert comps.session_layers.pending(WINDOW, SESSION) == 0


# ============================================================== 批量刷新


def test_refresh_candidates_finds_both_kinds(seeded):
    """有切片没概览的、与有概览但陈旧的，都该被找到。"""
    comps = seeded._components("work")
    rows = comps.session_layers.refresh_candidates()
    assert [(r["window_id"], r["session_id"]) for r in rows] == [(WINDOW, SESSION)]

    seeded.summarize_session(WINDOW, SESSION, db="work")
    assert comps.session_layers.refresh_candidates() == [], "已最新就不该再是候选"

    from duramem.models import ChunkDraft

    comps.repo.insert_chunk(
        ChunkDraft(summary_text="新的一条", source_window=WINDOW, source_session=SESSION)
    )
    assert len(comps.session_layers.refresh_candidates()) == 1


def test_refresh_sessions_batch(seeded):
    out = seeded.refresh_sessions(db="work")
    assert out["ok"] and out["candidates"] == 1 and out["refreshed"] == 1
    entry = out["results"][0]
    assert entry["session_id"] == SESSION and entry["ok"] is True

    # 再跑一次：已最新，不该重复生成
    again = seeded.refresh_sessions(db="work")
    assert again["candidates"] == 0 and again["refreshed"] == 0


def test_force_refreshes_what_the_policy_would_defer(seeded):
    """force 与策略的分工：宽会话攒着不刷时，force 能立刻刷。

    这条用"33 条切片 + 1 处变化"构造出策略会拦的局面（占比 3% < 10%），
    再验证 force 绕过它。
    """
    from duramem.models import ChunkDraft
    from duramem.session_summarizer import MARK_PENDING, REFRESH_NOW

    comps = seeded._components("work")
    for i in range(33):
        comps.repo.insert_chunk(
            ChunkDraft(
                summary_text=f"宽会话里的第 {i} 条内容",
                source_window=WINDOW,
                source_session=SESSION,
            )
        )
    seeded.summarize_session(WINDOW, SESSION, db="work")
    assert comps.session_layers.pending(WINDOW, SESSION) == 0

    # 只动一条：占比 1/34 ≈ 3%，策略应判"先记着"
    comps.repo.insert_chunk(
        ChunkDraft(summary_text="后来的一条", source_window=WINDOW, source_session=SESSION)
    )
    judged = comps.session_summarizer.maybe_refresh(WINDOW, SESSION)
    assert judged.decision == MARK_PENDING
    assert judged.summary is None, "被策略拦下时不该生成"

    forced = seeded.refresh_sessions(db="work", force=True)
    assert forced["refreshed"] == 1, "force 应绕过策略"
    assert comps.session_layers.pending(WINDOW, SESSION) == 0, "生成后计数应清零"
    assert REFRESH_NOW == "refresh_now"


# ============================================================== 会话层检索


def test_session_vector_index_is_populated_on_generation(seeded):
    comps = seeded._components("work")
    assert comps.session_index.count() == 0

    seeded.summarize_session(WINDOW, SESSION, db="work")
    assert comps.session_index.count() == 1, "生成概览时该顺手把 L0 嵌进去"


def test_session_hit_is_returned_for_a_matching_query(seeded):
    """会话层进检索后，命中会单独成段返回。

    说明测试边界：这条只验证**机制**（会话 L0 被索引、检索路能召回它）。
    "完全无关键词的冷启动"（"我们接着上次那个来"）依赖真语义模型——离线用的是
    词项重叠哈希，查询与会话摘要没有共同词就召不回，所以那种效果在这里验不了，
    不能拿这条测试冒充它。
    """
    seeded.summarize_session(WINDOW, SESSION, db="work")
    res = seeded.search("端口 许可证 向量库", db="work")
    assert res.sessions, "与该会话摘要重合的查询应命中会话"
    hit = res.sessions[0]
    assert hit.session_id == SESSION
    assert hit.window_id == WINDOW
    assert hit.abstract_text, "会话命中要带简介，模型据此判断要不要深入"
    assert hit.slice_count > 0


def test_session_hit_does_not_inline_the_overview(seeded):
    """会话命中**不带 L1 正文**——4000 token 随每个命中返回会把每轮灌爆。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    payload = seeded.search("端口 许可证", db="work").to_mcp_payload()
    for item in payload.get("sessions", []):
        assert "l1" not in item and "overview_text" not in item
        assert item["abstract"]
        assert "dm_read_session" in item["_suggestion"], "要告诉模型怎么取概览"


def test_sessions_key_absent_when_no_layers(seeded):
    """没有会话层时返回体里不该出现空的 sessions 段。"""
    payload = seeded.search("端口", db="work").to_mcp_payload()
    assert "sessions" not in payload
    assert "session_count" not in payload


def test_session_retrieval_failure_does_not_break_slice_search(seeded):
    """会话路炸了不能让切片检索跟着失败——降级要完整可用。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    comps = seeded._components("work")

    def boom(*args, **kwargs):
        raise RuntimeError("模拟会话检索故障")

    comps.pipeline.session_retriever.search = boom
    res = comps.pipeline.search("端口")
    assert res.hits, "切片结果仍应返回"
    assert any("会话检索失败" in w for w in res.warnings)
    assert res.sessions == []


def test_session_rerank_document_uses_the_l1_head(settings):
    """会话重排的打分体取 L1 的前若干 token（判别力最强的简述/覆盖度/导航）。"""
    from duramem.retrieval.sessions import rerank_document

    # 造一份明显超过预算的 L1（1200 token 默认值），确保真的触发截断
    long_l1 = "# 会话\n\n" + "简述内容。" * 300 + "\n\n## 导航\n\n- 想知道X → uid: abc"
    doc = rerank_document(long_l1, settings.session_rerank_max_tokens)
    assert doc.startswith("# 会话")
    assert len(doc) < len(long_l1), "该被预算截住"
    # 截断后仍应保住开头的简述
    assert "简述内容" in doc


def test_reindex_all_rebuilds_session_vectors(seeded):
    """重建向量时必须连会话向量一起重建。

    `rebuild_vectors` 会把两张向量表一起删掉重建（同一个嵌入提供方，只换一张
    会让库里混两种来源的向量），所以只重建切片的话会话向量会一直空着、
    冷启动那条路静默失效。
    """
    seeded.summarize_session(WINDOW, SESSION, db="work")
    comps = seeded._components("work")
    assert comps.session_index.count() == 1

    comps.session_index.clear()
    assert comps.session_index.count() == 0

    out = comps.pipeline.reindex_all()
    assert out["sessions_embedded"] == 1, "会话向量应被一起补回来"
    assert comps.session_index.count() == 1
    # 切片向量也要还在
    assert out["embedded"] > 0


def test_session_debug_comes_from_the_same_run(seeded):
    """调试面板的排名必须与返回结果同源——重跑一遍会让两者不一致。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    comps = seeded._components("work")
    res = comps.pipeline.search("端口 许可证", collect_debug=True)
    if not res.sessions:
        pytest.skip("离线嵌入下该查询未命中会话")
    assert res.debug.get("session_fusion_pool"), "调试信息应来自同一次检索"


# ============================================================== 会话层读取


def test_read_session_returns_l0_and_overview(seeded):
    seeded.summarize_session(WINDOW, SESSION, db="work")
    res = seeded.read_session(SESSION, db="work")
    assert res.ok
    assert res.window_id == WINDOW
    assert res.abstract_text and res.overview
    assert res.overview.lstrip().startswith("# 会话")
    assert res.slice_count > 0
    assert res.tokens == res.tokens_this_round, "本轮第一次读，累计应等于本次"
    payload = res.to_payload()
    assert payload["abstract"] and payload["overview"]
    assert payload["session_id"] == SESSION


def test_read_session_abstract_detail_is_cheap(seeded):
    """l0 档用于"先看看这个会话跟我有关吗"的探路。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    full = seeded.read_session(SESSION, db="work")
    light = seeded.read_session(SESSION, detail="abstract", db="work")
    assert light.ok
    assert "overview" not in light.to_payload(), "l0 档不该带概览正文"
    assert light.tokens < full.tokens


def test_read_session_invalid_detail_reports_choices(seeded):
    seeded.summarize_session(WINDOW, SESSION, db="work")
    res = seeded.read_session(SESSION, detail="全部", db="work")
    assert res.ok is False
    assert any("不支持的 detail" in w for w in res.warnings)


def test_read_session_without_layer_explains_how_to_make_one(seeded):
    res = seeded.read_session("sess-nonexistent", db="work")
    assert res.ok is False
    assert any("没有该会话的概览" in w for w in res.warnings)
    assert any("refresh_sessions" in w for w in res.warnings), "要告诉调用方怎么生成"


def test_read_session_switch_blocks_it(seeded):
    """L1 开关关掉后会话概览不可读，且提示里要说清是哪个设置项。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    assert seeded.read_session(SESSION, db="work").ok

    assert seeded.update_settings({"overview_access_enabled": False})["applied"]
    blocked = seeded.read_session(SESSION, db="work")
    assert blocked.ok is False
    assert any("OVERVIEW_ACCESS_ENABLED" in w for w in blocked.warnings)

    # 但 L2 不受影响——两个开关是独立的
    uid = seeded.search("端口", db="work").hits[0].uid
    assert seeded.read_original(uid, db="work", mode="full").ok

    seeded.update_settings({"overview_access_enabled": True})
    assert seeded.read_session(SESSION, db="work").ok


def test_l2_switch_does_not_block_session_overview(seeded):
    """反过来也要成立：关掉原文不该连带关掉概览。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    seeded.update_settings({"original_access_enabled": False})
    assert seeded.read_session(SESSION, db="work").ok

    uid = seeded.search("端口", db="work").hits[0].uid
    assert seeded.read_original(uid, db="work", mode="full").ok is False


def test_slice_suggestion_mentions_the_session_entry(seeded):
    """切片命中要提示会话入口——否则模型不知道还能往上走一层。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    hits = seeded.search("端口", db="work").hits
    assert hits
    assert any("dm_read_session" in (h.suggestion or "") for h in hits)


def test_suggestion_hides_session_entry_when_switch_is_off(seeded):
    """开关关掉时不能提示——否则模型会去调一个必然被拒的工具。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    seeded.update_settings({"overview_access_enabled": False})
    hits = seeded.search("端口", db="work").hits
    assert hits
    assert not any("dm_read_session" in (h.suggestion or "") for h in hits)


def test_read_session_counts_toward_the_round(seeded):
    """概览读取也要计入取回量，让模型看得见本轮花了多少。"""
    seeded.summarize_session(WINDOW, SESSION, db="work")
    seeded.search("端口", db="work")  # 开新一轮
    first = seeded.read_session(SESSION, detail="abstract", db="work")
    second = seeded.read_session(SESSION, db="work")
    assert first.tokens_this_round == first.tokens
    assert second.tokens_this_round == first.tokens + second.tokens
