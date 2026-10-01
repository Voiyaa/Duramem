"""历史对话导入的测试。

这批测试的核心不是"能导入"，而是三个更容易出错的地方：
1. **只读**——源库正在被宿主使用，任何写入都可能损坏它
2. **幂等**——反复导入同一份历史不该产生重复记忆
3. **噪声剔除**——宿主注入的提醒和助手的过程叙述不是知识，
   实测占导入内容的 83%，混进记忆会直接毁掉检索质量
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from duramem.importers import detect_importer, get_importer, list_available
from duramem.importers.base import (
    flatten_content,
    ms_to_iso,
    normalize_role,
    timestamp_to_ms,
    window_from_directory,
)
from duramem.importers.transcripts import (
    ClaudeCodeImporter,
    GenericJsonImporter,
    ZCodeRolloutImporter,
)
from duramem.importers.zcode_db import ZCodeDbImporter, _assistant_text, _is_synthetic

# ====================================================================== 造一个假源库

def make_zcode_db(path: Path, sessions: list[dict]) -> Path:
    """按实测到的 ZCode 表结构造一个最小源库。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT, parent_id TEXT,
            slug TEXT, directory TEXT, path TEXT, title TEXT, version TEXT, share_url TEXT,
            summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER,
            summary_diffs TEXT, revert TEXT, permission TEXT, time_created INTEGER,
            time_updated INTEGER, time_compacting INTEGER, time_archived INTEGER,
            task_type TEXT, title_source TEXT, title_message_id TEXT, time_title_updated INTEGER,
            trace_id TEXT
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT, sequence INTEGER
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER,
            time_updated INTEGER, data TEXT, sequence INTEGER
        );
        """
    )
    for session in sessions:
        conn.execute(
            "INSERT INTO session(id, directory, title, task_type, time_created, project_id) "
            "VALUES (?,?,?,?,?,?)",
            (
                session["id"],
                session.get("directory", "E:/work/proj"),
                session.get("title", "测试会话"),
                session.get("task_type", "interactive"),
                session.get("time_created", 1790000000000),
                "proj_test",
            ),
        )
        for index, item in enumerate(session["messages"]):
            message_id = f"msg_{session['id']}_{index}"
            meta: dict = {"role": item["role"], "time": {"created": 1790000000000 + index}}
            if item.get("synthetic"):
                meta["synthetic"] = True
                meta["semantics"] = {"origin": "agent_runtime", "kind": "todo_reminder"}
            conn.execute(
                "INSERT INTO message(id, session_id, data, sequence) VALUES (?,?,?,?)",
                (message_id, session["id"], json.dumps(meta), index),
            )
            # parts 按 item["parts"] 给，便于构造 step 结构
            for pindex, part in enumerate(item["parts"]):
                conn.execute(
                    "INSERT INTO part(id, message_id, session_id, data, sequence) "
                    "VALUES (?,?,?,?,?)",
                    (
                        f"part_{message_id}_{pindex}",
                        message_id,
                        session["id"],
                        json.dumps(part),
                        pindex,
                    ),
                )
    conn.commit()
    conn.close()
    return path


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def stop_step(text: str) -> list[dict]:
    """一个以 stop 结束的 step —— 最终回答。"""
    return [{"type": "step-start"}, text_part(text), {"type": "step-finish", "reason": "stop"}]


def tool_step(text: str) -> list[dict]:
    """一个以 tool-calls 结束的 step —— 工具调用前的叙述。"""
    return [{"type": "step-start"}, text_part(text), {"type": "step-finish", "reason": "tool-calls"}]


# ====================================================================== 纯函数


def test_normalize_role_maps_known_vocabularies():
    assert normalize_role("user") == "user"
    assert normalize_role("Human") == "user"
    assert normalize_role("assistant") == "assistant"
    assert normalize_role("MODEL") == "assistant"
    # tool / system 进记忆只会变成噪声，必须返回 None
    assert normalize_role("tool") is None
    assert normalize_role("system") is None
    assert normalize_role(None) is None


def test_flatten_content_drops_non_text_parts():
    """图片与工具结果不能进记忆——它们会污染索引。"""
    assert flatten_content("纯文本") == "纯文本"
    assert flatten_content([{"type": "text", "text": "甲"}, {"type": "text", "text": "乙"}]) == "甲\n乙"
    assert flatten_content([{"type": "image_url", "image_url": {"url": "x"}}]) == ""
    assert flatten_content([{"type": "tool_result", "content": "工具输出"}]) == ""
    assert flatten_content(None) == ""


def test_window_from_directory_separates_projects():
    assert window_from_directory("E:\\work\\ChatAgent", "zcode") == "zcode:ChatAgent"
    assert window_from_directory("/home/me/proj", "claude") == "claude:proj"
    assert window_from_directory(None, "generic") == "generic:default"


def test_timestamp_handles_seconds_and_millis():
    assert timestamp_to_ms(1790000000) == 1790000000000
    assert timestamp_to_ms(1790000000000) == 1790000000000
    assert timestamp_to_ms("1790000000000") == 1790000000000
    assert timestamp_to_ms("2026-09-23T12:00:00Z") is not None
    assert timestamp_to_ms("不是时间") is None
    assert ms_to_iso(None) is None


# ====================================================================== step 结构


def test_assistant_text_keeps_final_answer_drops_narration():
    """以 stop 结束的 step 是最终回答，以 tool-calls 结束的是过程叙述。"""
    chunks = [*tool_step("我先看看这个文件"), *stop_step("结论是端口被占了")]
    final, narration = _assistant_text(chunks)
    assert final == "结论是端口被占了"
    assert narration == "我先看看这个文件"


def test_assistant_text_without_step_markers_keeps_everything():
    """结构变了或更早的版本没有 step 标记时保守保留，只在结构明确时才丢。"""
    final, narration = _assistant_text([text_part("没有 step 标记的正文")])
    assert final == "没有 step 标记的正文"
    assert narration == ""


def test_assistant_text_keeps_unfinished_last_step():
    """最后一步没收到 step-finish（流被中断）时保守保留。"""
    chunks = [{"type": "step-start"}, text_part("没写完的回答")]
    final, _ = _assistant_text(chunks)
    assert final == "没写完的回答"


def test_assistant_text_with_no_text_parts():
    final, narration = _assistant_text([{"type": "tool", "tool": "Bash"}])
    assert final == "" and narration == ""


@pytest.mark.parametrize(
    "meta",
    [
        {"role": "user", "synthetic": True},
        {"role": "user", "semantics": {"origin": "agent_runtime"}},
        {"role": "user", "visibility": "model-only"},
        {"role": "user", "anchor": {"origin": "synthetic"}},
    ],
)
def test_synthetic_detection_covers_all_observed_markers(meta):
    """宿主注入的提醒有四种实测标记，任一种都应被识别出来。"""
    assert _is_synthetic(meta) is True


def test_real_user_message_is_not_synthetic():
    assert _is_synthetic({"role": "user", "semantics": {"origin": "real_user"}}) is False
    assert _is_synthetic({"role": "user"}) is False


# ====================================================================== 适配器


def test_probe_recognizes_zcode_db(tmp_path):
    path = make_zcode_db(
        tmp_path / "db.sqlite",
        [{"id": "s1", "messages": [{"role": "user", "parts": [text_part("你好")]}]}],
    )
    assert ZCodeDbImporter.probe(path) is True
    # 别的文件不该被误认
    other = tmp_path / "other.sqlite"
    sqlite3.connect(other).execute("CREATE TABLE t(x)")
    assert ZCodeDbImporter.probe(other) is False


def test_zcode_db_load_marks_noise_as_system(tmp_path):
    path = make_zcode_db(
        tmp_path / "db.sqlite",
        [
            {
                "id": "s1",
                "messages": [
                    {"role": "user", "parts": [text_part("真实提问")]},
                    {"role": "user", "synthetic": True, "parts": [text_part("这是宿主注入的提醒")]},
                    {"role": "assistant", "parts": tool_step("我先看看")},
                    {"role": "assistant", "parts": stop_step("真实回答")},
                ],
            }
        ],
    )
    conversations = list(ZCodeDbImporter().load(path))
    assert len(conversations) == 1
    roles = [(m.role, m.speaker, m.content) for m in conversations[0].messages]

    assert ("user", None, "真实提问") in roles
    assert ("assistant", None, "真实回答") in roles
    # 噪声保留在库里（L1 完整性），但标成 system 且带来源
    assert ("system", "todo_reminder", "这是宿主注入的提醒") in roles
    assert ("system", "assistant_narration", "我先看看") in roles


def test_zcode_db_excludes_subagents_by_default(tmp_path):
    """实测 501 个会话里 439 个是子代理——那是主代理派出去的任务，不是用户的对话。"""
    path = make_zcode_db(
        tmp_path / "db.sqlite",
        [
            {"id": "real", "task_type": "interactive",
             "messages": [{"role": "user", "parts": [text_part("我的提问")]}]},
            {"id": "sub", "task_type": "subagent_child",
             "messages": [{"role": "user", "parts": [text_part("Research task...")]}]},
        ],
    )
    default = list(ZCodeDbImporter().load(path))
    assert [c.origin_id for c in default] == ["real"]

    with_sub = list(ZCodeDbImporter().load(path, include_subagents=True))
    assert {c.origin_id for c in with_sub} == {"real", "sub"}


def test_zcode_db_source_is_opened_read_only(tmp_path):
    """源库正被宿主使用，适配器**必须**只读打开——写入可能损坏它。"""
    path = make_zcode_db(
        tmp_path / "db.sqlite",
        [{"id": "s1", "messages": [{"role": "user", "parts": [text_part("内容")]}]}],
    )
    before = path.read_bytes()
    list(ZCodeDbImporter().load(path))
    with pytest.raises(sqlite3.OperationalError):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("INSERT INTO session(id) VALUES ('injected')")
    assert path.read_bytes() == before


def test_session_id_is_preserved_so_hook_and_import_merge(tmp_path):
    """导入用源会话 id 原样做 session_id：这样 hook 实时采集与历史导入
    会落到同一个会话上，而不是各建一个、互相看不见。"""
    path = make_zcode_db(
        tmp_path / "db.sqlite",
        [{"id": "sess_abc", "messages": [{"role": "user", "parts": [text_part("内容")]}]}],
    )
    conversation = next(iter(ZCodeDbImporter().load(path)))
    assert conversation.session_id == "sess_abc"
    assert conversation.messages[0].session_id == "sess_abc"


def test_zcode_db_reports_messages_without_text(tmp_path):
    """纯工具调用的轮次没有任何文本，应被跳过而不是塞空消息。"""
    path = make_zcode_db(
        tmp_path / "db.sqlite",
        [
            {
                "id": "s1",
                "messages": [
                    {"role": "assistant", "parts": [{"type": "tool", "tool": "Bash"}]},
                    {"role": "user", "parts": [text_part("有内容")]},
                ],
            }
        ],
    )
    conversation = next(iter(ZCodeDbImporter().load(path)))
    assert [m.content for m in conversation.messages] == ["有内容"]


# ====================================================================== rollout


def make_rollout(path: Path, session_id: str, rounds: list[dict]) -> Path:
    lines = []
    for item in rounds:
        lines.append(
            json.dumps(
                {
                    "type": "model_io",
                    "sessionId": session_id,
                    "startedAt": "2026-09-23T12:00:00Z",
                    "completedAt": "2026-09-23T12:00:05Z",
                    "request": {"messages": item.get("messages", []), "messageCount": len(item.get("messages", [])), "messagesKind": "tail"},
                    "response": {"text": item.get("response", "")},
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_rollout_probe_and_cumulative_dedup(tmp_path):
    """rollout 的 request.messages 是**累积**的（还可能是被截断的窗口），
    所以必须按内容去重、只收没见过的，否则同一句话会被写进去很多遍。"""
    path = make_rollout(
        tmp_path / "model-io-sess_x.jsonl",
        "sess_x",
        [
            {"messages": [{"role": "user", "content": "第一个问题"}], "response": "第一个回答"},
            {
                "messages": [
                    {"role": "user", "content": "第一个问题"},
                    {"role": "assistant", "content": "第一个回答"},
                    {"role": "user", "content": "第二个问题"},
                ],
                "response": "第二个回答",
            },
        ],
    )
    assert ZCodeRolloutImporter.probe(path) is True
    conversation = next(iter(ZCodeRolloutImporter().load(path)))
    contents = [m.content for m in conversation.messages]
    assert contents == ["第一个问题", "第一个回答", "第二个问题", "第二个回答"]


def test_rollout_skips_tool_messages(tmp_path):
    path = make_rollout(
        tmp_path / "model-io-sess_y.jsonl",
        "sess_y",
        [
            {
                "messages": [
                    {"role": "user", "content": "问题"},
                    {"role": "tool", "content": "工具输出"},
                ],
                "response": "回答",
            }
        ],
    )
    contents = [m.content for m in next(iter(ZCodeRolloutImporter().load(path))).messages]
    assert "工具输出" not in contents


# ====================================================================== Claude Code


def test_claude_code_probe_and_load(tmp_path):
    project = tmp_path / "projects" / "-home-me-proj"
    project.mkdir(parents=True)
    file = project / "abc-123.jsonl"
    file.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in [
                {"type": "user", "sessionId": "cc-1", "cwd": "/home/me/proj",
                 "message": {"role": "user", "content": "帮我看看端口"}},
                {"type": "assistant", "sessionId": "cc-1",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "端口被占了"}]}},
                {"type": "user", "sessionId": "cc-1",
                 "message": {"role": "user", "content": [{"type": "image", "source": "x"}]}},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert ClaudeCodeImporter.probe(file) is True
    conversation = next(iter(ClaudeCodeImporter().load(file)))
    assert conversation.session_id == "cc-1"
    assert conversation.window_id == "claude:proj"
    assert [m.content for m in conversation.messages] == ["帮我看看端口", "端口被占了"]


# ====================================================================== 通用


def test_generic_jsonl_and_array_shapes(tmp_path):
    jsonl = tmp_path / "chat.jsonl"
    jsonl.write_text(
        json.dumps({"role": "user", "content": "问题", "session_id": "g1"}) + "\n"
        + json.dumps({"role": "assistant", "text": "回答", "session_id": "g1"}) + "\n",
        encoding="utf-8",
    )
    assert GenericJsonImporter.probe(jsonl) is True
    conversation = next(iter(GenericJsonImporter().load(jsonl)))
    assert [m.content for m in conversation.messages] == ["问题", "回答"]

    arr = tmp_path / "chat.json"
    arr.write_text(
        json.dumps([{"role": "user", "content": "数组形态"}], ensure_ascii=False),
        encoding="utf-8",
    )
    assert [m.content for m in next(iter(GenericJsonImporter().load(arr))).messages] == ["数组形态"]


def test_generic_supports_custom_field_names(tmp_path):
    """自制导出文件的字段名五花八门，要容错。"""
    path = tmp_path / "custom.jsonl"
    path.write_text(
        json.dumps({"speaker": "我", "message": "自定义字段"}) + "\n", encoding="utf-8"
    )
    assert [m.content for m in next(iter(GenericJsonImporter().load(path))).messages] == ["自定义字段"]


# ====================================================================== 探测优先级


def test_detect_prefers_specific_over_generic(tmp_path):
    path = make_zcode_db(
        tmp_path / "db.sqlite",
        [{"id": "s1", "messages": [{"role": "user", "parts": [text_part("x")]}]}],
    )
    # 通用适配器只认 .json/.jsonl，这里验证顺序不会把专用源误判
    assert detect_importer(path).name == "zcode-db"

    rollout = make_rollout(tmp_path / "model-io-sess_z.jsonl", "sess_z", [{"messages": [], "response": "x"}])
    assert detect_importer(rollout).name == "zcode-rollout"


def test_get_importer_aliases():
    assert get_importer("zcode").name == "zcode-db"
    assert get_importer("claude").name == "claude-code"
    assert get_importer("json").name == "generic"
    with pytest.raises(ValueError):
        get_importer("不存在的源")


# ====================================================================== 列出来给用户挑


def test_list_available_reports_already_imported(tmp_path):
    path = make_zcode_db(
        tmp_path / "db.sqlite",
        [
            {"id": "done", "title": "已导入的", "messages": [{"role": "user", "parts": [text_part("a")]}]},
            {"id": "todo", "title": "还没导的", "messages": [{"role": "user", "parts": [text_part("b")]}]},
        ],
    )
    source, resolved, items = list_available(
        path=str(path), source="zcode", imported_ids={"done"}
    )
    assert source == "zcode-db" and resolved == str(path)
    by_id = {item.session_id: item for item in items}
    assert by_id["done"].already_imported is True
    assert by_id["todo"].already_imported is False
    assert by_id["todo"].messages == 1


def test_list_available_rejects_unknown_format(tmp_path):
    junk = tmp_path / "x.bin"
    junk.write_bytes(b"\x00\x01\x02")
    with pytest.raises(ValueError) as excinfo:
        list_available(path=str(junk))
    assert "--source" in str(excinfo.value)


# ====================================================================== 服务层端到端


def test_import_is_idempotent(service, tmp_path):
    """反复导入同一份历史不该产生重复记忆。"""
    path = make_zcode_db(
        tmp_path / "db.sqlite",
        [
            {
                "id": "sess_idem",
                "messages": [
                    {"role": "user", "parts": [text_part("第一句提问")]},
                    {"role": "assistant", "parts": stop_step("第一句回答")},
                ],
            }
        ],
    )

    first = service.import_conversations(path=str(path), source="zcode", db="work")
    assert first.conversations_imported == 1
    assert first.messages_imported == 2
    after_first = service.stats("work")["messages"]

    second = service.import_conversations(path=str(path), source="zcode", db="work")
    assert second.messages_imported == 2, "会再走一遍写入，但不应新增行"
    assert service.stats("work")["messages"] == after_first


def test_import_only_selected_conversations(service, tmp_path):
    """用户挑中的才导——这是这个功能的重点，不能一不小心全灌进去。"""
    path = make_zcode_db(
        tmp_path / "db.sqlite",
        [
            {"id": "want", "title": "想要的", "messages": [
                {"role": "user", "parts": [text_part("选中这条")]},
                {"role": "assistant", "parts": stop_step("选中这条的回答")},
            ]},
            {"id": "skip", "title": "不要的", "messages": [
                {"role": "user", "parts": [text_part("不要这条")]},
                {"role": "assistant", "parts": stop_step("不要这条的回答")},
            ]},
        ],
    )
    report = service.import_conversations(
        path=str(path), source="zcode", db="work", sessions=["want"]
    )
    assert report.conversations_imported == 1

    repo = service._components("work").repo
    sessions = {row["session_id"] for row in repo.sessions()}
    assert "want" in sessions and "skip" not in sessions


def test_summarize_skips_non_conversational_messages(service, tmp_path):
    """噪声不进摘要：宿主提醒与助手叙述不是知识，切进记忆会毁掉检索质量。"""
    path = make_zcode_db(
        tmp_path / "db.sqlite",
        [
            {
                "id": "sess_noise",
                "messages": [
                    {"role": "user", "parts": [text_part("用户的真实问题")]},
                    {"role": "user", "synthetic": True, "parts": [text_part("宿主注入的提醒正文")]},
                    {"role": "assistant", "parts": tool_step("我先查一下")},
                    {"role": "assistant", "parts": stop_step("用户的真实回答")},
                ],
            }
        ],
    )
    service.import_conversations(path=str(path), source="zcode", db="work")
    repo = service._components("work").repo
    # window_id 由来源目录派生（zcode:<目录名>），测试里不能瞎猜，直接读库里的
    window = repo.sessions()[0]["window_id"]
    outcome = service.summarize(window, "sess_noise", db="work")
    assert outcome.ok, outcome.warnings
    chunks = repo.list_chunks(limit=50)
    assert chunks
    for chunk in chunks:
        assert "宿主注入的提醒正文" not in chunk["summary_text"]
        assert "我先查一下" not in chunk["summary_text"]
        # 但原文里应当保留（L1 区间渲染包含一切）——不过 L1 只渲染对话内容，
        # 所以这里断言的是"摘要干净"
    joined_l0 = " ".join(chunk["summary_text"] for chunk in chunks)
    assert "用户的真实问题" in joined_l0 or "用户的真实回答" in joined_l0


def test_max_messages_keeps_original_seq(service, tmp_path):
    """只取最近 N 条时必须保留原始 seq：重新编号会让"先导入最近 N 条、
    之后再导入全部"产生两套互不重合的位置，同一批消息被写两组。"""
    messages = []
    for i in range(10):
        messages.append({"role": "user", "parts": [text_part(f"第 {i} 句提问")]})
        messages.append({"role": "assistant", "parts": stop_step(f"第 {i} 句回答")})
    path = make_zcode_db(tmp_path / "db.sqlite", [{"id": "sess_trim", "messages": messages}])

    report = service.import_conversations(
        path=str(path), source="zcode", db="work", max_messages=4
    )
    assert report.conversations_imported == 1

    repo = service._components("work").repo
    rows = repo.db.read_conn.execute(
        "SELECT seq, content FROM messages WHERE session_id='sess_trim' ORDER BY seq"
    ).fetchall()
    seqs = [row["seq"] for row in rows]
    assert len(seqs) == 4
    assert max(seqs) == 19, "seq 应是它在原会话里的位置（末尾），不是 0..3"

    # 之后再导入全部：已有的不应重复，只补上缺的
    total_before = service.stats("work")["messages"]
    service.import_conversations(path=str(path), source="zcode", db="work")
    total_after = service.stats("work")["messages"]
    assert total_after - total_before == 16, "只补还没导入的 16 条"

# ====================================================================== DeepSeek Harness（dsh）


def _dsh_header(session_id: str, created_ms: int, cwd: str) -> dict:
    return {
        "type": "session",
        "version": 3,
        "id": session_id,
        "createdAt": created_ms,
        "cwd": cwd,
        "isSeeded": False,
        "delegationDepth": 0,
    }


def _dsh_event(event_type: str, seq: int, time_ms: int, data: dict, **extra) -> dict:
    event = {"type": event_type, "seq": seq, "time": time_ms, "data": data}
    event.update(extra)
    return event


def _dsh_user(text: str, mid: str, kind: str = "user") -> dict:
    return {
        "role": "user",
        "id": mid,
        "source": {"kind": kind, "plugin": "fs"} if kind != "user" else {"kind": kind},
        "content": [{"type": "text", "text": text}],
    }


def _dsh_assistant(text: str, mid: str, blocks=None) -> dict:
    return {
        "role": "assistant",
        "id": mid,
        "source": {"kind": "model"},
        "content": blocks if blocks is not None else [{"type": "text", "text": text}],
    }


def _dsh_sample_records() -> list[dict]:
    """一段最小但形状完整的 dsh 会话：真人输入、注入、助手文本、工具往返、标题。"""
    return [
        _dsh_header("session-aaa", 1789000000000, "E:\\proj work space"),
        _dsh_event("turn/start", 1, 1789000001000, {"turn": 0}),
        _dsh_event(
            "user/message", 2, 1789000001100, {"message": _dsh_user("帮我看看端口占用", "m1")},
            surfaceOp="append",
        ),
        _dsh_event(
            "user/message", 3, 1789000001200,
            {"message": _dsh_user("文件变更通知", "m2", kind="plugin")},
            surfaceOp="append",
        ),
        _dsh_event(
            "assistant/message", 4, 1789000002000,
            {"turn": 0, "step": 0, "message": _dsh_assistant("端口 8001 被占了", "m3")},
            surfaceOp="append",
        ),
        _dsh_event(
            "tool/call", 5, 1789000002100,
            {"turn": 0, "step": 0, "callId": "call-1", "name": "netstat", "arguments": "{}"},
        ),
        _dsh_event(
            "tool/result", 6, 1789000002500,
            {
                "turn": 0,
                "step": 0,
                "message": {
                    "role": "user",
                    "id": "m4",
                    "source": {"kind": "tool"},
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": "call-1",
                            "content": [{"type": "text", "text": "PID 1234 LISTENING"}],
                        }
                    ],
                },
            },
            surfaceOp="append",
        ),
        _dsh_event(
            "assistant/message", 7, 1789000003000,
            {
                "turn": 0,
                "step": 1,
                "message": _dsh_assistant("", "m5", blocks=[
                    {"type": "tool-call", "id": "call-2", "name": "taskkill", "arguments": "{}"},
                ]),
            },
            surfaceOp="append",
        ),
        _dsh_event(
            "session/title", 8, 1789000003100,
            {"title": "端口占用排查", "messageSeqs": [2], "source": {"kind": "provider", "provider": "fixture"}},
        ),
        _dsh_event("turn/end", 9, 1789000003200, {"turn": 0, "reason": {"kind": "completed"}}),
    ]


def _write_dsh_log(path: Path, records: list[dict], compress: bool = True) -> None:
    """按 dsh 的真实落盘方式写：多帧拼接的 zstd（或明文）。帧界刻意切在字节中段。"""
    raw = ("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n").encode("utf-8")
    if not compress:
        path.write_bytes(raw)
        return
    import zstandard

    compressor = zstandard.ZstdCompressor()
    cut = len(raw) // 2
    path.write_bytes(compressor.compress(raw[:cut]) + compressor.compress(raw[cut:]))


def _write_dsh_session_tree(
    tmp_path: Path, records: list[dict], *, compress: bool = True, version: int = 3
) -> Path:
    session_dir = tmp_path / "sessions" / "--E-proj--" / "session-aaa"
    session_dir.mkdir(parents=True)
    suffix = ".zstd" if compress else ""
    _write_dsh_log(session_dir / f"session.v{version}.jsonl{suffix}", records, compress=compress)
    return session_dir


def test_dsh_probe_and_load(tmp_path):
    from duramem.importers.dsh import DshSessionImporter

    session_dir = _write_dsh_session_tree(tmp_path, _dsh_sample_records())
    importer = DshSessionImporter()
    assert importer.probe(session_dir) is True
    assert importer.probe(tmp_path / "sessions") is True

    conversation = next(iter(importer.load(tmp_path / "sessions")))
    assert conversation.session_id == "session-aaa"
    assert conversation.window_id == "dsh:proj work space"
    assert conversation.title == "端口占用排查"
    assert [m.role for m in conversation.messages] == [
        "user", "system", "assistant", "system", "assistant",
    ], "注入内容与工具结果归 system，不进 L1 渲染"
    assert [m.content for m in conversation.messages] == [
        "帮我看看端口占用",
        "文件变更通知",
        "端口 8001 被占了",
        "[工具结果 netstat]\nPID 1234 LISTENING",
        "[调用工具 taskkill]",  # 纯工具调用的助手轮渲染成一行，不产生空消息
    ]
    assert conversation.messages[0].source_msg_id == "2", "source_msg_id 用事件 seq"
    assert conversation.metadata["is_subagent"] is False
    assert "unknown_event_types" not in conversation.metadata


def test_dsh_plaintext_log_also_loads(tmp_path):
    from duramem.importers.dsh import DshSessionImporter

    session_dir = _write_dsh_session_tree(tmp_path, _dsh_sample_records(), compress=False)
    conversation = next(iter(DshSessionImporter().load(session_dir)))
    assert [m.content for m in conversation.messages][0] == "帮我看看端口占用"


def test_dsh_compaction_replaces_shadowed_surface(tmp_path):
    """压缩走通用 surfaceOp=replace：被遮蔽的区间移除，摘要消息顶上。"""
    from duramem.importers.dsh import DshSessionImporter

    records = _dsh_sample_records()
    records.append(
        _dsh_event(
            "compaction/summary", 10, 1789000004000,
            {
                "compactionId": "c1",
                "summary": [],
                "shadowedRange": {"start": 2, "end": 5},
                "shadowedSeqs": [2, 3, 4, 5],
                "shadowedTokenCount": 100,
                "provider": "p",
                "model": "m",
            },
        )
    )
    records.append(
        _dsh_event(
            "user/message", 11, 1789000004100,
            {"message": _dsh_user("（以上对话的压缩摘要）", "m6", kind="plugin")},
            surfaceOp={"op": "replace", "startSeq": 2, "endSeq": 5},
        )
    )
    session_dir = _write_dsh_session_tree(tmp_path, records)
    conversation = next(iter(DshSessionImporter().load(session_dir)))
    contents = [m.content for m in conversation.messages]
    assert "帮我看看端口占用" not in contents, "被遮蔽的真人消息应从表面移除"
    assert "端口 8001 被占了" not in contents
    assert "（以上对话的压缩摘要）" in contents
    assert "[工具结果 netstat]\nPID 1234 LISTENING" in contents, "区间之外的消息保留"


def test_dsh_unknown_event_recorded_not_fatal(tmp_path):
    from duramem.importers.dsh import DshSessionImporter

    records = _dsh_sample_records()
    records.append(_dsh_event("brand-new/thing", 20, 1789000005000, {"x": 1}))
    records.append(_dsh_event("info/thing", 21, 1789000005001, {"x": 1}, ignorable=True))
    session_dir = _write_dsh_session_tree(tmp_path, records)
    conversation = next(iter(DshSessionImporter().load(session_dir)))
    assert conversation.messages, "未知事件不阻塞导入"
    assert conversation.metadata["unknown_event_types"] == ["brand-new/thing"], (
        "非 ignorable 的未知类型要如实记录，ignorable 的按契约直接跳过"
    )


def test_dsh_subagent_excluded_by_default(tmp_path):
    from duramem.importers.dsh import DshSessionImporter

    records = _dsh_sample_records()
    records[0] = {**records[0], "origin": "subagent", "delegationDepth": 1}
    session_dir = _write_dsh_session_tree(tmp_path, records)
    importer = DshSessionImporter()
    assert list(importer.load(session_dir)) == []
    found = list(importer.load(session_dir, include_subagents=True))
    assert len(found) == 1 and found[0].metadata["is_subagent"] is True


def test_dsh_unsupported_newer_version_falls_back_to_older_generation(tmp_path):
    """同一会话目录并存 v4（本工具不认）与 v3：回落到 v3，不丢会话。"""
    from duramem.importers.dsh import DshSessionImporter

    session_dir = _write_dsh_session_tree(tmp_path, _dsh_sample_records(), version=3)
    _write_dsh_log(
        session_dir / "session.v4.jsonl.zstd",
        [{**_dsh_header("session-aaa", 1789000009000, "E:\\proj work space"), "version": 4}],
    )
    conversation = next(iter(DshSessionImporter().load(session_dir)))
    assert conversation.metadata["generation"] == "session.v3.jsonl.zstd"


def test_dsh_since_ms_skips_sessions_without_new_events(tmp_path):
    from duramem.importers.dsh import DshSessionImporter

    session_dir = _write_dsh_session_tree(tmp_path, _dsh_sample_records())
    importer = DshSessionImporter()
    assert list(importer.load(session_dir, since_ms=1789000006000)) == [], "最后事件早于游标"
    assert len(list(importer.load(session_dir, since_ms=1000))) == 1


def test_dsh_registered_with_sources_and_defaults():
    from duramem.importers import known_defaults, list_sources

    assert "dsh" in {item["name"] for item in list_sources()}
    assert get_importer("dsh").name == "dsh"
    assert "dsh" in {item["source"] for item in known_defaults()}
