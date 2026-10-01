"""记忆网关测试：采集原文、注入记忆、流式直通。

用 httpx.MockTransport 假造上游，不需要任何真实 API。
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from duramem.collectors import normalize_content, openai_messages_to_messages
from duramem.collectors.gateway import build_memory_block, create_gateway_router


@pytest.fixture
def gateway(seeded, monkeypatch):
    """把网关挂到一个 TestClient 上，上游换成假的。"""
    from fastapi import FastAPI

    upstream_calls: list[dict] = []
    state = {"mode": "ok"}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["mode"] == "fail":
            raise httpx.ConnectError("上游不可达")
        body = json.loads(request.content)
        upstream_calls.append(
            {"url": str(request.url), "body": body, "headers": dict(request.headers)}
        )
        if body.get("stream"):
            lines = [
                'data: {"choices":[{"delta":{"content":"端口"}}]}',
                'data: {"choices":[{"delta":{"content":"被占用了"}}]}',
                "data: [DONE]",
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=("\n\n".join(lines) + "\n\n").encode(),
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "上游回答：改 BACKEND_PORT 即可"}}
                ]
            },
        )

    app = FastAPI()
    app.include_router(
        create_gateway_router(
            service=seeded,
            upstream_base_url="https://upstream.test/v1",
            upstream_api_key="test-key",
            default_db="work",
        )
    )
    client = TestClient(app)
    # 让测试能切换上游行为，而不必重复 monkeypatch（重复 patch 会自我递归）
    client.upstream_state = state

    original_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    return client, upstream_calls, seeded


# ====================================================================== 纯函数


def test_normalize_content_variants():
    assert normalize_content("纯文本") == "纯文本"
    assert normalize_content([{"type": "text", "text": "甲"}, {"type": "text", "text": "乙"}]) == "甲\n乙"
    assert normalize_content([{"type": "image_url", "image_url": {"url": "x"}}]) == ""
    assert normalize_content(None) == ""


def test_openai_messages_use_stable_seq_across_trimming():
    """客户端会从头裁掉旧消息。若按数组下标分配 seq，裁剪后同一位置内容变了，
    按 (window, session, seq) 去重会互相覆盖——所以必须按内容反查 seq。"""
    known = {"问题A": 0, "回答A": 1, "问题B": 2}

    def lookup(window, session, text, index):
        return known.get(text, max(known.values()) + 1)

    # 第二轮请求里，客户端已经裁掉了最早的"问题A"
    trimmed = [
        {"role": "assistant", "content": "回答A"},
        {"role": "user", "content": "问题B"},
    ]
    msgs = openai_messages_to_messages(trimmed, "w", "s", seq_lookup=lookup)
    assert [m.seq for m in msgs] == [1, 2], "裁剪后 seq 应保持稳定，不能变成 0/1"


def test_openai_messages_skip_empties():
    payload = [
        {"role": "user", "content": "有内容"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
        "不是字典",
    ]
    msgs = openai_messages_to_messages(payload, "w", "s")
    assert len(msgs) == 1 and msgs[0].content == "有内容"


# ====================================================================== 采集


def test_gateway_collects_messages_and_reply(gateway):
    client, calls, seeded = gateway
    body = {
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "网关采集测试：错误码 GW_COLLECT_0xabc 怎么处理"},
        ]
    }
    response = client.post(
        "/v1/chat/completions", json=body, headers={"x-duramem-session": "gw-s1"}
    )
    assert response.status_code == 200
    assert response.json()["_duramem"]["collected"]["accepted"] >= 1

    # 上游确实被调用了
    assert calls and calls[0]["url"].endswith("/chat/completions")

    # 请求和回复都落库了
    stats = seeded.stats("work")
    assert stats["messages"] >= 2
    repo = seeded._components("work").repo
    contents = repo.get_messages(range(1, 200))
    joined = " ".join(m["content"] for m in contents)
    assert "GW_COLLECT_0xabc" in joined
    assert "改 BACKEND_PORT 即可" in joined


def test_gateway_injects_memory_into_system_prompt(gateway):
    client, calls, seeded = gateway
    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "之前那个错误码 ERR_CONN_REFUSED_0x7f 怎么解决的"}]},
        headers={"x-duramem-session": "gw-s2"},
    )
    sent = calls[-1]["body"]
    system = sent["messages"][0]
    assert system["role"] == "system"
    assert "长期记忆" in system["content"]
    assert "dm_read_original" in system["content"], "注入块应告诉模型如何取原文"
    assert "uid:" in system["content"], "应带上 uid 供回查"


def test_gateway_preserves_existing_system_prompt(gateway):
    client, calls, seeded = gateway
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "你是严谨的助手"},
                {"role": "user", "content": "端口 ERR_CONN_REFUSED_0x7f"},
            ]
        },
        headers={"x-duramem-session": "gw-s3"},
    )
    content = calls[-1]["body"]["messages"][0]["content"]
    assert content.startswith("你是严谨的助手"), "原有 system 内容必须保留"
    assert len(calls[-1]["body"]["messages"]) == 2, "不应新增 system 消息"


def test_gateway_inject_memory_switch_is_read_live(gateway):
    """「网关注入记忆」是运行时项，关掉必须立刻停注，不能吃路由创建时的快照。"""
    client, calls, seeded = gateway
    seeded.settings.gateway_inject_memory = False
    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "ERR_CONN_REFUSED_0x7f"}]},
        headers={"x-duramem-session": "gw-live-off"},
    )
    messages = calls[-1]["body"]["messages"]
    assert all("长期记忆" not in (m.get("content") or "") for m in messages if m["role"] == "system")

    seeded.settings.gateway_inject_memory = True
    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "ERR_CONN_REFUSED_0x7f"}]},
        headers={"x-duramem-session": "gw-live-on"},
    )
    system = calls[-1]["body"]["messages"][0]
    assert system["role"] == "system" and "长期记忆" in system["content"]


def test_gateway_forwards_caller_auth_header(gateway):
    client, calls, seeded = gateway
    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "认证透传测试"}]},
        headers={"authorization": "Bearer caller-key", "x-duramem-session": "gw-s4"},
    )
    assert calls[-1]["headers"]["authorization"] == "Bearer caller-key"


def test_gateway_falls_back_to_configured_key(gateway):
    client, calls, seeded = gateway
    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "回退测试"}]},
        headers={"x-duramem-session": "gw-s5"},
    )
    assert calls[-1]["headers"]["authorization"] == "Bearer test-key"


def test_gateway_upstream_failure_returns_502_not_crash(gateway):
    """上游挂了要给 502，而不是让网关自己崩——记忆服务不该让用户没法聊天。"""
    client, calls, seeded = gateway
    client.upstream_state["mode"] = "fail"
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "上游挂了"}]},
        headers={"x-duramem-session": "gw-s6"},
    )
    assert response.status_code == 502
    assert "上游请求失败" in response.json()["error"]["message"]


def test_gateway_bad_json_returns_400(gateway):
    client, _, _ = gateway
    response = client.post(
        "/v1/chat/completions",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400


# ====================================================================== 流式


def test_gateway_streams_and_collects_reply(gateway):
    client, calls, seeded = gateway
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "stream": True,
            "messages": [{"role": "user", "content": "流式采集测试 STREAMMARK_0x1"}],
        },
        headers={"x-duramem-session": "gw-stream"},
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())

    assert "data: " in text and "[DONE]" in text
    assert calls[-1]["body"]["stream"] is True

    # 流结束后应把拼起来的回复落库
    repo = seeded._components("work").repo
    joined = " ".join(m["content"] for m in repo.get_messages(range(1, 300)))
    assert "STREAMMARK_0x1" in joined
    assert "端口被占用了" in joined, "流式分片应被拼回完整回复后入库"


# ====================================================================== 会话指纹


def test_session_fingerprint_is_stable_for_same_conversation(gateway):
    """客户端不传 session 时，用首条 user 消息指纹推断会话，多次请求应归同一会话。"""
    client, calls, seeded = gateway
    body = {"messages": [{"role": "user", "content": "会话指纹测试 FINGERPRINT_0x9"}]}
    client.post("/v1/chat/completions", json=body)
    client.post("/v1/chat/completions", json=body)

    sessions = seeded._components("work").repo.sessions()
    fingerprints = [s["session_id"] for s in sessions if s["session_id"].startswith("gw-")]
    assert len(fingerprints) == 1, f"两次相同请求应归同一会话，实际 {fingerprints}"


def test_explicit_session_header_wins(gateway):
    client, calls, seeded = gateway
    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "显式会话"}]},
        headers={"x-duramem-session": "explicit-session"},
    )
    sessions = seeded._components("work").repo.sessions()
    assert "explicit-session" in [s["session_id"] for s in sessions]


# ====================================================================== 注入内容


def test_build_memory_block_empty_without_user_message(seeded):
    assert build_memory_block({"messages": []}, seeded, "work") == ""
    assert build_memory_block({"messages": [{"role": "assistant", "content": "x"}]}, seeded, "work") == ""


def test_memory_block_marks_summaries_as_summaries(seeded):
    """注入块必须说清"这是摘要"，否则模型会把摘要当原文引用。"""
    block = build_memory_block(
        {"messages": [{"role": "user", "content": "ERR_CONN_REFUSED_0x7f"}]}, seeded, "work"
    )
    assert "摘要" in block
    assert "不要臆测" in block or "不" in block
