"""摘要生成测试——含设计文档的验收标准「区间指针准确性」。

关键设计：original_text 与 original_char_start/end **由服务从 msg 区间自己推导**，
不采信模型给的字符数。模型能准确判断"哪几条消息属于这个记忆"，
但让它数字符必然错。
"""

from __future__ import annotations

import pytest
from conftest import SESSION, WINDOW, dialogue_messages

from duramem.models import Message, render_messages_with_offsets
from duramem.store.repository import Repository
from duramem.summarizer import (
    HeuristicSummarizer,
    Summarizer,
    SummaryError,
    extract_json,
)

# ====================================================================== JSON 解析


def test_extract_json_handles_fences():
    assert extract_json('```json\n{"chunks": []}\n```') == {"chunks": []}
    assert extract_json('好的：{"chunks": [{"a": 1}]} 完成') == {"chunks": [{"a": 1}]}


def test_extract_json_raises_on_garbage():
    with pytest.raises(SummaryError):
        extract_json("这里没有 JSON")
    with pytest.raises(SummaryError):
        extract_json("")


# ====================================================================== 区间指针


def test_interval_pointers_are_accurate(seeded):
    """验收标准：每条切片的 original_text 必须等于其消息区间的规范化渲染，
    且 original_char_start/original_char_end 必须精确切出 original_text。抽样全部切片核对。"""
    components = seeded._components("work")
    repo = components.repo
    chunks = repo.list_chunks(limit=1000)
    assert chunks, "应有切片可供核对"

    for chunk in chunks:
        start, end = chunk["msg_id_start"], chunk["msg_id_end"]
        assert start is not None and end is not None
        assert start <= end

        rows = repo.get_message_range(start, end)
        assert rows, f"切片 {chunk['chunk_uid']} 的区间里没有消息"

        expected, offsets = render_messages_with_offsets(rows)
        assert chunk["original_text"] == expected, f"{chunk['chunk_uid']} 的 original_text 与区间渲染不一致"

        assert chunk["original_char_start"] == offsets[0][1]
        assert chunk["original_char_end"] == offsets[-1][2]
        assert chunk["original_text"][chunk["original_char_start"] : chunk["original_char_end"]] == chunk["original_text"]


def test_all_messages_are_covered_by_some_chunk(seeded):
    """总结应覆盖游标之后的所有消息，不漏。"""
    repo = seeded._components("work").repo
    total = repo.message_count(WINDOW, SESSION)
    covered: set[int] = set()
    for chunk in repo.list_chunks(limit=1000):
        if chunk["msg_id_start"] is None:
            continue
        covered.update(range(chunk["msg_id_start"], chunk["msg_id_end"] + 1))
    assert len(covered) == total


def test_char_offsets_survive_message_edit(seeded):
    """宿主编辑了消息后，引用它的切片必须同步重渲染 original_text 与字符区间。

    否则 original_text 会变成一份过期的原文快照，"切片作为原文索引"就名不副实，
    区间指针也不再可校验。
    """
    repo = seeded._components("work").repo
    messages = dialogue_messages()
    messages[5] = Message(WINDOW, SESSION, 5, "assistant", "改成了完全不同的结论")
    repo.upsert_messages(messages)

    # 引用消息 5 的切片，其 original_text 必须已经跟着变
    affected = [
        chunk
        for chunk in repo.list_chunks(limit=1000)
        if chunk["msg_id_start"] <= 6 <= chunk["msg_id_end"]
    ]
    assert affected, "应有切片覆盖被修改的消息"
    for chunk in affected:
        assert "改成了完全不同的结论" in chunk["original_text"]

    # 全局不变式仍然成立
    for chunk in repo.list_chunks(limit=1000):
        rows = repo.get_message_range(chunk["msg_id_start"], chunk["msg_id_end"])
        assert chunk["original_text"] == render_messages_with_offsets(rows)[0]


def test_edit_does_not_rewrite_l0_summary(seeded):
    """只更新引用的原文，不改 l0 摘要——摘要是模型当时的判断，不该被静默改写。"""
    repo = seeded._components("work").repo
    before = {chunk["chunk_uid"]: chunk["summary_text"] for chunk in repo.list_chunks(limit=1000)}

    messages = dialogue_messages()
    messages[1] = Message(WINDOW, SESSION, 1, "assistant", "改了一句话")
    repo.upsert_messages(messages)

    after = {chunk["chunk_uid"]: chunk["summary_text"] for chunk in repo.list_chunks(limit=1000)}
    assert before == after


# ====================================================================== 校验


class ScriptedSummarizer:
    """返回预设内容的假模型，用于测试各种异常输出。

    接受两种形态：直接的 chunk 列表，或 {"chunks": [...]} 字典
    （后者与真实 provider 的解析路径一致，便于测试畸形输出）。
    """

    model = "scripted"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def summarize(self, messages, max_chunks, settings):
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        if callable(self.payload):
            return self.payload(messages)
        if isinstance(self.payload, dict):
            return self.payload.get("chunks", [])
        return self.payload


def _summarizer(service, payload) -> tuple[Summarizer, ScriptedSummarizer]:
    components = service._components("work")
    provider = ScriptedSummarizer(payload)
    summarizer = Summarizer(
        components.repo, components.pipeline.embedder, components.index,
        service.settings, provider=provider,
    )
    return summarizer, provider


def test_out_of_range_message_ids_are_rejected(service):
    repo = Repository(service._components("work").db, service.segmenter)
    repo.upsert_messages(dialogue_messages())
    summarizer, _ = _summarizer(
        service,
        [
            {"summary_text": "合法切片", "msg_id_start": 1, "msg_id_end": 3},
            {"summary_text": "越界切片", "msg_id_start": 900, "msg_id_end": 999},
            {"summary_text": "缺区间切片"},
        ],
    )
    outcome = summarizer.summarize(WINDOW, SESSION)

    assert outcome.added == 1
    assert outcome.rejected == 2
    assert any("越界" in w for w in outcome.warnings)
    assert any("缺少" in w or "缺失" in w for w in outcome.warnings)


def test_reversed_range_is_normalized(service):
    repo = Repository(service._components("work").db, service.segmenter)
    repo.upsert_messages(dialogue_messages())
    summarizer, _ = _summarizer(
        service, [{"summary_text": "反着的区间", "msg_id_start": 5, "msg_id_end": 2}]
    )
    outcome = summarizer.summarize(WINDOW, SESSION)
    assert outcome.added == 1
    chunk = repo.list_chunks(limit=1)[0]
    assert chunk["msg_id_start"] == 2 and chunk["msg_id_end"] == 5


def test_model_supplied_l1_is_overridden_with_warning(service):
    """模型自述的原文若与区间渲染不一致，以库内消息为准并记录告警。"""
    repo = Repository(service._components("work").db, service.segmenter)
    repo.upsert_messages(dialogue_messages())
    summarizer, _ = _summarizer(
        service,
        [{"summary_text": "摘要", "msg_id_start": 1, "msg_id_end": 2,
          "original_text": "模型自己改写的原文，并不逐字"}],
    )
    outcome = summarizer.summarize(WINDOW, SESSION)
    assert outcome.added == 1
    assert any("不一致" in w for w in outcome.warnings)

    chunk = repo.list_chunks(limit=1)[0]
    assert "模型自己改写的原文" not in chunk["original_text"]
    assert chunk["original_text"] == render_messages_with_offsets(
        repo.get_message_range(1, 2)
    )[0]


def test_empty_l0_is_rejected(service):
    repo = Repository(service._components("work").db, service.segmenter)
    repo.upsert_messages(dialogue_messages())
    summarizer, _ = _summarizer(
        service, [{"summary_text": "  ", "msg_id_start": 1, "msg_id_end": 2}]
    )
    outcome = summarizer.summarize(WINDOW, SESSION)
    assert outcome.added == 0
    assert outcome.rejected == 1


def test_invalid_json_shape_yields_no_chunks(service):
    repo = Repository(service._components("work").db, service.segmenter)
    repo.upsert_messages(dialogue_messages())
    summarizer, _ = _summarizer(service, {"chunks": ["字符串不是对象", 42]})
    outcome = summarizer.summarize(WINDOW, SESSION)
    assert outcome.added == 0
    assert outcome.rejected == 2


def test_provider_failure_is_recorded_not_raised(service):
    repo = Repository(service._components("work").db, service.segmenter)
    repo.upsert_messages(dialogue_messages())
    summarizer, _ = _summarizer(service, SummaryError("模拟摘要接口 500"))
    outcome = summarizer.summarize(WINDOW, SESSION)

    assert outcome.ok is False
    assert any("500" in w for w in outcome.warnings)
    runs = repo.summary_runs()
    assert runs and runs[0]["chunks_added"] == 0
    assert repo.get_cursor(WINDOW, SESSION) == -1, "失败时游标不应前进"


# ====================================================================== 游标与去重


def test_cursor_prevents_duplicate_summarization(service):
    repo = Repository(service._components("work").db, service.segmenter)
    repo.upsert_messages(dialogue_messages())
    summarizer, _ = _summarizer(
        service, [{"summary_text": "第一次总结", "msg_id_start": 1, "msg_id_end": 4}]
    )

    first = summarizer.summarize(WINDOW, SESSION)
    assert first.added == 1
    assert repo.get_cursor(WINDOW, SESSION) == 4

    second = summarizer.summarize(WINDOW, SESSION)
    assert second.added == 0, "游标之后的增量不足，不应重复总结已处理的消息"


def test_two_windows_sharing_a_db_do_not_double_summarize(service):
    """跨窗口场景：游标按 (window, session) 维护，两个窗口各自推进。"""
    repo = Repository(service._components("work").db, service.segmenter)
    repo.upsert_messages(dialogue_messages())
    repo.upsert_messages(dialogue_messages(window="win-2"))
    summarizer, _ = _summarizer(
        service,
        lambda messages: [
            {"summary_text": f"窗口 {messages[0]['window_id']} 的总结",
             "msg_id_start": messages[0]["id"], "msg_id_end": messages[-1]["id"]}
        ],
    )

    a = summarizer.summarize(WINDOW, SESSION)
    b = summarizer.summarize("win-2", SESSION)
    assert a.added == 1 and b.added == 1
    assert a.chunk_uids != b.chunk_uids
    assert repo.count_chunks(alive_only=True) == 2

    # 再各跑一次，两边都无新增
    assert summarizer.summarize(WINDOW, SESSION).added == 0
    assert summarizer.summarize("win-2", SESSION).added == 0


def test_identical_content_is_deduplicated(service):
    repo = Repository(service._components("work").db, service.segmenter)
    repo.upsert_messages(dialogue_messages())
    summarizer, _ = _summarizer(
        service,
        lambda messages: [
            {"summary_text": "完全一样的摘要内容",
             "msg_id_start": messages[0]["id"], "msg_id_end": messages[0]["id"]},
            {"summary_text": "完全一样的摘要内容",
             "msg_id_start": messages[1]["id"], "msg_id_end": messages[1]["id"]},
        ],
    )
    outcome = summarizer.summarize(WINDOW, SESSION)
    assert outcome.added == 1
    assert outcome.duplicates == 1


def test_insufficient_messages_is_refused(service):
    repo = Repository(service._components("work").db, service.segmenter)
    repo.upsert_messages(dialogue_messages()[:1])
    summarizer, _ = _summarizer(service, [])
    outcome = summarizer.summarize(WINDOW, SESSION)
    assert outcome.ok is False
    assert any("不足" in w for w in outcome.warnings)


# ====================================================================== 离线实现


def test_heuristic_summarizer_produces_valid_ranges(service):
    components = service._components("work")
    components.repo.upsert_messages(dialogue_messages())
    provider = HeuristicSummarizer(service.segmenter, group_size=4)
    rows = [dict(r) for r in components.db.read_conn.execute(
        "SELECT * FROM messages ORDER BY id"
    ).fetchall()]

    chunks = provider.summarize(rows, max_chunks=8, settings=service.settings)
    assert chunks
    valid_ids = {row["id"] for row in rows}
    for chunk in chunks:
        assert chunk["msg_id_start"] in valid_ids
        assert chunk["msg_id_end"] in valid_ids
        assert chunk["msg_id_start"] <= chunk["msg_id_end"]
        assert chunk["summary_text"].strip()
        assert isinstance(chunk["keywords"], list)


def test_summary_payload_shape(seeded):
    payload = seeded.summarize(WINDOW, SESSION, db="work").to_payload()
    for field in ("ok", "added", "duplicates", "rejected", "model_used", "duration_ms", "uids"):
        assert field in payload
