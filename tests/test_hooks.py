"""hook 采集器测试。

两个要点：
1. 宿主文档没有给出 hook 的 stdin 载荷结构，所以解析必须**容错**而不是猜死一个字段名。
2. `hook-ingest` 的退出码必须恒为 0、stdout 必须干净——采集失败不该阻塞用户会话，
   而 stdout 有杂音会让整次 hook 运行被判为失败（ZCode hook 规范）。
"""

from __future__ import annotations

import io
import json

from duramem.hooks import log_raw, parse_hook_payload, run_hook

# ====================================================================== 解析


def test_user_prompt_extracted_from_various_field_names():
    """不同宿主的用词不同，不能只认一个字段名。"""
    for key in ("prompt", "user_prompt", "userPrompt", "message", "text", "content"):
        result = parse_hook_payload({key: "帮我看看端口占用"}, "UserPromptSubmit")
        assert len(result.messages) == 1, f"{key} 未被识别"
        assert result.messages[0].content == "帮我看看端口占用"
        assert result.messages[0].role == "user"


def test_assistant_response_extracted_from_various_field_names():
    for key in ("response", "assistant_response", "last_response", "preview", "message"):
        result = parse_hook_payload({key: "改 BACKEND_PORT 即可"}, "Stop")
        assert len(result.messages) == 1, f"{key} 未被识别"
        assert result.messages[0].role == "assistant"


def test_nested_content_shape_is_handled():
    """有些宿主把内容包成 {"message": {"content": "..."}}。"""
    result = parse_hook_payload(
        {"message": {"role": "user", "content": "嵌套形状的输入"}}, "UserPromptSubmit"
    )
    assert result.messages and result.messages[0].content == "嵌套形状的输入"


def test_empty_payload_reports_warning_not_crash():
    result = parse_hook_payload({}, "UserPromptSubmit")
    assert result.messages == []
    assert any("没有可识别的文本" in w for w in result.warnings)


def test_non_dict_payload_is_handled():
    result = parse_hook_payload(["不是对象"], "Stop")  # type: ignore[arg-type]
    assert result.messages == []
    assert result.warnings


def test_session_and_window_from_payload():
    result = parse_hook_payload(
        {"prompt": "内容", "session_id": "sess-abc", "cwd": "E:/work/proj"},
        "UserPromptSubmit",
    )
    assert result.messages[0].session_id == "sess-abc"
    # 窗口名与导入侧同源（`<来源>:<目录名>`），不是原始 cwd——
    # 唯一键含 window_id，两条采集路名字不一致就等于同一次会话存两份。
    assert result.messages[0].window_id == "zcode:proj"


def test_hook_window_matches_importer_window():
    """hook 采集与历史导入必须落在同一个窗口里。"""
    from duramem.importers.base import window_from_directory

    payload = {"prompt": "内容", "session_id": "s", "cwd": "E:/work/proj"}
    hook_window = parse_hook_payload(payload, "UserPromptSubmit").messages[0].window_id
    assert hook_window == window_from_directory("E:/work/proj", "zcode")


def test_window_falls_back_when_payload_has_no_cwd():
    result = parse_hook_payload({"prompt": "内容"}, "UserPromptSubmit")
    assert result.messages[0].window_id == "zcode:default"


def test_explicit_session_and_window_override_payload():
    result = parse_hook_payload(
        {"prompt": "内容", "session_id": "from-payload", "cwd": "/x"},
        "UserPromptSubmit",
        window_id="my-window",
        session_id="my-session",
    )
    assert result.messages[0].session_id == "my-session"
    assert result.messages[0].window_id == "my-window"


def test_session_is_stable_without_session_id():
    """没有 session_id 时，同一载荷应生成同一会话标识，否则每次 hook 都是新会话。"""
    payload = {"prompt": "同一个输入"}
    first = parse_hook_payload(payload, "UserPromptSubmit").messages[0].session_id
    second = parse_hook_payload(payload, "UserPromptSubmit").messages[0].session_id
    assert first == second

    other = parse_hook_payload({"prompt": "另一个输入"}, "UserPromptSubmit")
    assert other.messages[0].session_id != first


def test_transcript_file_is_read_when_present(tmp_path):
    """转录文件能一次补齐整段对话，比逐条拼更完整，优先用它。"""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in [
                {"role": "user", "content": "第一个问题"},
                {"role": "assistant", "content": "第一个回答"},
                {"type": "user", "content": "第二个问题"},
                {"role": "user", "content": [{"type": "text", "text": "数组形式的内容"}]},
                "不是对象",
                {"role": "user", "content": "   "},
            ]
        ),
        encoding="utf-8",
    )
    result = parse_hook_payload(
        {"transcript_path": str(transcript)}, "Stop", session_id="s1"
    )
    contents = [m.content for m in result.messages]
    assert contents == ["第一个问题", "第一个回答", "第二个问题", "数组形式的内容"]
    assert [m.seq for m in result.messages] == [0, 1, 2, 3]


def test_unreadable_transcript_falls_back_to_payload(tmp_path):
    result = parse_hook_payload(
        {"transcript_path": str(tmp_path / "missing.jsonl"), "response": "兜底文本"},
        "Stop",
    )
    assert [m.content for m in result.messages] == ["兜底文本"]
    assert any("读不出消息" in w for w in result.warnings)


# ====================================================================== 原始载荷落盘


def test_raw_payload_is_logged(tmp_path):
    """这是这一层最有价值的产出：宿主没给载荷结构，先如实记下来。"""
    path = log_raw(tmp_path, "UserPromptSubmit", {"prompt": "原文"})
    assert path.endswith("UserPromptSubmit.jsonl")

    log_raw(tmp_path, "UserPromptSubmit", {"prompt": "第二条"})
    lines = (tmp_path / "hooks" / "UserPromptSubmit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["prompt"] == "原文"


# ====================================================================== 端到端


def test_run_hook_writes_to_the_database(tmp_path):
    data_dir = tmp_path / "data"
    payload = json.dumps(
        {"session_id": "hooked", "cwd": "E:/proj", "prompt": "端口 ERR_CONN_REFUSED_0x7f 怎么解决"}
    )
    result = run_hook(
        "UserPromptSubmit",
        data_dir,
        db=None,
        stdin_stream=io.StringIO(payload),
    )
    assert result["ok"] is True
    assert result["collected"] == 1
    assert (data_dir / "hooks" / "UserPromptSubmit.jsonl").exists()

    # 数据真的进了库
    from duramem.config import Settings
    from duramem.service import Service

    settings = Settings()
    settings.data_dir = data_dir
    settings.embedding_fake = True
    service = Service(settings)
    try:
        sessions = service._components(service.ensure_default_db()).repo.sessions()
        assert sessions and sessions[0]["session_id"] == "hooked"
    finally:
        service.close()


def test_run_hook_never_raises_on_bad_stdin(tmp_path):
    """采集失败不该阻塞用户的会话，所以永远返回 ok 并把问题写进 warnings。"""
    result = run_hook("Stop", tmp_path / "data", stdin_stream=io.StringIO("这不是 JSON"))
    assert result["ok"] is True
    assert result["collected"] == 0
    assert any("不是合法 JSON" in w for w in result["warnings"])


def test_run_hook_survives_empty_stdin(tmp_path):
    result = run_hook("Stop", tmp_path / "data", stdin_stream=io.StringIO(""))
    assert result["ok"] is True
    assert result["collected"] == 0


def test_run_hook_on_unusable_data_dir_still_returns_ok(tmp_path):
    """数据目录不可用（比如指向一个文件）时也只能降级，不能抛。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("我是个文件，不是目录", encoding="utf-8")
    result = run_hook(
        "Stop",
        blocker / "data",
        stdin_stream=io.StringIO(json.dumps({"response": "内容"})),
    )
    assert result["ok"] is True
    assert result["collected"] == 0
    assert result["warnings"]


# ====================================================================== CLI


def test_cli_hook_ingest_keeps_stdout_clean(tmp_path, capsys):
    """ZCode 规范：hook 的 stdout 要么是合法 JSON 要么为空，
    多余输出会让整次 hook 运行被判为失败。"""
    from duramem.__main__ import main
    from duramem.config import reset_settings

    reset_settings()
    payload = json.dumps({"prompt": "CLI 采集测试 STDINMARK_0x1", "session_id": "cli-hook"})
    import sys as _sys

    original = _sys.stdin
    _sys.stdin = io.StringIO(payload)
    try:
        code = main(["hook-ingest", "--event", "UserPromptSubmit", "--data-dir", str(tmp_path)])
    finally:
        _sys.stdin = original
        reset_settings()

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "", f"stdout 必须干净，实际输出：{captured.out!r}"
