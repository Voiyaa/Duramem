"""OpenAI 兼容记忆网关。

把 Duramem 挂到模型 API 前面，原文天然流经它——**这是接入成本最低的采集路线，
不需要任何宿主配合**（能改 base_url 的客户端都能用）。

它同时做两件事：

1. **采集**：请求里的 messages 落库为原始消息，响应里的 assistant 回复也落库
2. **注入**（可选）：转发前检索记忆，把相关切片拼进 system prompt

第 2 件事是"被动模式"：模型没有工具调用权，但每轮都能看到相关记忆。
配合 MCP 的"主动模式"（模型自己决定 `dm_read_original`）两条路径都在，
因为它们拿到的能力不同——网关拿得到原文、MCP 给得了主动权。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from duramem import net
from duramem.collectors import normalize_content, openai_messages_to_messages
from duramem.service import DuramemError, Service

MEMORY_BLOCK_HEADER = (
    "以下是从长期记忆中检索到的相关内容。它们是**摘要**，"
    "如果需要具体数值、原话、错误码或文件名等细节，"
    "应使用 dm_read_original 工具查看原文；没有相关记忆时不要臆测。"
)


def _session_key(
    payload: dict[str, Any], window_header: str | None, session_header: str | None
) -> tuple[str, str]:
    """推断窗口与会话标识。

    客户端通常不传会话 ID，所以退化为：用第一条 user 消息的哈希作为会话指纹。
    同一段对话每次请求的第一条消息相同（或被裁掉后换了新的首条），
    因此这个指纹在多数客户端上足够稳定；显式传 header 时以 header 为准。
    """
    window = window_header or str(payload.get("user") or "gateway")
    if session_header:
        return window, session_header

    import hashlib

    first_user = next(
        (
            normalize_content(item.get("content"))
            for item in payload.get("messages", [])
            if isinstance(item, dict) and item.get("role") == "user"
        ),
        "",
    )
    digest = hashlib.sha256(first_user.encode("utf-8")).hexdigest()[:12]
    return window, f"gw-{digest}"


def build_memory_block(payload: dict[str, Any], service: Service, db: str | None) -> str:
    """用最近的用户消息检索记忆，拼成待注入的文本块。"""
    query = ""
    for item in reversed(payload.get("messages", [])):
        if isinstance(item, dict) and item.get("role") == "user":
            query = normalize_content(item.get("content"))
            if query.strip():
                break
    if not query.strip():
        return ""

    try:
        result = service.search(query, db=db, collect_debug=False)
    except DuramemError:
        return ""
    if not result.hits:
        return ""

    lines = [MEMORY_BLOCK_HEADER, ""]
    for hit in result.hits:
        label = f"[{hit.title}] " if hit.title else ""
        lines.append(f"- {label}{hit.summary_text} (uid: {hit.uid})")
    if result.suggestion:
        lines.append("")
        lines.append(result.suggestion)
    return "\n".join(lines)


def create_gateway_router(
    service: Service,
    upstream_base_url: str,
    upstream_api_key: str,
    default_db: str | None = None,
    timeout: float = 300.0,
) -> APIRouter:
    # 是否注入记忆**不作为参数捕获**：它读 service.settings.gateway_inject_memory
    # （设置页的运行时项，跨进程热重载），捕获成布尔值会让界面上的开关失灵。
    router = APIRouter()

    def _target(path: str) -> str:
        return f"{upstream_base_url.rstrip('/')}/{path.lstrip('/')}"

    def _upstream_headers(request: Request) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        # 优先透传调用方自己的 Key，其次用配置的
        auth = request.headers.get("authorization")
        if auth:
            headers["authorization"] = auth
        elif upstream_api_key:
            headers["authorization"] = f"Bearer {upstream_api_key}"
        return headers

    def _collect(
        payload: dict[str, Any],
        window: str,
        session: str,
        db: str | None,
    ) -> dict[str, Any]:
        """把请求里的消息落库（含 seq 稳定分配）。

        **同步函数，调用方必须经 `run_in_threadpool` 执行**：内部可能触发
        auto_summarize（摘要模型调用超时 120s）与检索（嵌入/重排的网络往返）。
        这些活一旦跑在事件循环线程上，本进程的所有并发请求与流式转发一起停摆
        ——REST 与 MCP 侧都进了线程池，这条路径不能是例外。
        """
        repo = service._components(db or service.ensure_default_db()).repo

        def seq_lookup(w: str, s: str, text: str, index: int) -> int:
            found = repo.find_message_seq_by_hash(w, s, text)
            if found is not None:
                return found
            return repo.max_message_seq(w, s) + 1

        messages = openai_messages_to_messages(
            payload.get("messages") or [], window, session, seq_lookup=seq_lookup
        )
        if not messages:
            return {"accepted": 0}
        return service.ingest(messages, db=db, auto_summarize=True)

    def _inject(payload: dict[str, Any], block: str) -> dict[str, Any]:
        """把记忆块插到 system prompt 里（保留已有 system 内容）。"""
        messages = list(payload.get("messages") or [])
        for index, item in enumerate(messages):
            if isinstance(item, dict) and item.get("role") == "system":
                existing = normalize_content(item.get("content"))
                merged = f"{existing}\n\n{block}" if existing else block
                messages[index] = {**item, "content": merged}
                return {**payload, "messages": messages}
        return {**payload, "messages": [{"role": "system", "content": block}, *messages]}

    # ==================================================================

    @router.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        x_duramem_window: str | None = Header(default=None),
        x_duramem_session: str | None = Header(default=None),
        x_duramem_db: str | None = Header(default=None),
    ):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400, content={"error": {"message": "请求体不是合法 JSON"}}
            )

        db = x_duramem_db or default_db
        window, session = _session_key(payload, x_duramem_window, x_duramem_session)

        collect_info: dict[str, Any] = {}
        try:
            collect_info = await run_in_threadpool(_collect, payload, window, session, db)
        except Exception as exc:  # noqa: BLE001
            # 采集失败不能影响对话本身——记忆挂了不该让用户没法聊天
            collect_info = {"error": f"采集失败：{exc}"}

        outgoing = payload
        if service.settings.gateway_inject_memory:
            # 检索含嵌入/重排的网络往返，同样不能占事件循环
            block = await run_in_threadpool(build_memory_block, payload, service, db)
            if block:
                outgoing = _inject(payload, block)
                collect_info["injected_memories"] = block.count("\n- ")

        streaming = bool(payload.get("stream"))
        headers = _upstream_headers(request)

        if not streaming:
            # 客户端每次请求自建（AsyncClient 绑定 event loop，不能跨 loop 共享），
            # 但 SSL 上下文必须用进程级共享的那份——否则每个转发请求都要重新解析
            # 一遍 235KB 的 CA bundle，实测 3.4s/请求。上下文构建本身约 0.9s，
            # 首次要经线程池拿（返回后是缓存读，开销可忽略）：本模块的原则是
            # 重活不进事件循环，一次性的 0.9s 也不例外。
            context = await run_in_threadpool(net.ssl_context)
            async with httpx.AsyncClient(timeout=timeout, verify=context) as client:
                try:
                    response = await client.post(
                        _target("chat/completions"), json=outgoing, headers=headers
                    )
                except httpx.HTTPError as exc:
                    return JSONResponse(
                        status_code=502,
                        content={"error": {"message": f"上游请求失败：{exc}"}},
                    )
            await run_in_threadpool(_collect_reply, response, window, session, db, collect_info)
            try:
                body = response.json()
            except Exception:
                body = {"error": {"message": response.text[:500]}}
            body.setdefault("_duramem", {})
            if isinstance(body.get("_duramem"), dict):
                body["_duramem"] = {"collected": collect_info}
            return JSONResponse(status_code=response.status_code, content=body)

        async def stream() -> AsyncIterator[bytes]:
            collected_text: list[str] = []
            context = await run_in_threadpool(net.ssl_context)
            async with httpx.AsyncClient(timeout=timeout, verify=context) as client:
                try:
                    async with client.stream(
                        "POST", _target("chat/completions"), json=outgoing, headers=headers
                    ) as response:
                        if response.status_code >= 400:
                            raw = await response.aread()
                            yield raw
                            return
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            if line.startswith("data: "):
                                data = line[6:]
                                if data.strip() != "[DONE]":
                                    collected_text.append(_delta_text(data))
                            yield (line + "\n\n").encode("utf-8")
                except httpx.HTTPError as exc:
                    error = json.dumps(
                        {"error": {"message": f"上游请求失败：{exc}"}}, ensure_ascii=False
                    )
                    yield f"data: {error}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                    return

            reply = "".join(collected_text)
            if reply.strip():
                # 流已结束、事件循环还活着：落库（可能含 SQLite 写）别占它
                await run_in_threadpool(_persist_reply, reply, window, session, db, collect_info)

        return StreamingResponse(stream(), media_type="text/event-stream")

    def _collect_reply(
        response: httpx.Response,
        window: str,
        session: str,
        db: str | None,
        info: dict[str, Any],
    ) -> None:
        """解析回复并落库。同步函数，调用方必须经 `run_in_threadpool` 执行。"""
        if response.status_code >= 400:
            return
        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"]
            reply = normalize_content(text)
        except Exception:
            return
        if reply.strip():
            _persist_reply(reply, window, session, db, info)

    def _persist_reply(
        reply: str, window: str, session: str, db: str | None, info: dict[str, Any]
    ) -> None:
        """回复落库。同步函数（内含 SQLite 写），调用方必须经 `run_in_threadpool` 执行。"""
        try:
            from duramem.models import Message

            repo = service._components(db or service.ensure_default_db()).repo
            existing = repo.find_message_seq_by_hash(window, session, reply)
            if existing is not None:
                return
            seq = repo.max_message_seq(window, session) + 1
            service.ingest(
                [
                    Message(
                        window_id=window,
                        session_id=session,
                        seq=seq,
                        role="assistant",
                        content=reply,
                    )
                ],
                db=db,
                auto_summarize=False,
            )
            info["reply_collected"] = True
        except Exception as exc:  # noqa: BLE001
            info["reply_error"] = str(exc)

    def _delta_text(data: str) -> str:
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return ""
        try:
            delta = chunk["choices"][0].get("delta") or {}
        except (KeyError, IndexError):
            return ""
        return normalize_content(delta.get("content"))

    # ==================================================================

    @router.post("/v1/messages")
    async def anthropic_messages(
        request: Request,
        x_duramem_window: str | None = Header(default=None),
        x_duramem_session: str | None = Header(default=None),
        x_duramem_db: str | None = Header(default=None),
    ):
        """Anthropic Messages API 的最小实现：只做采集直通。

        有意不做记忆注入——注入策略需要按该 API 的 system 字段结构单独设计，
        而它能承载的客户端主要是 Claude Code，那里更适合走 hook 路线。
        """
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400, content={"error": {"message": "请求体不是合法 JSON"}}
            )

        db = x_duramem_db or default_db
        window, session = _session_key(payload, x_duramem_window, x_duramem_session)
        # 本路由只直通不注入（见 docstring），采集的返回值无处可用；
        # 调用的意义在副作用（消息落库），失败也不该挡住转发。
        try:
            await run_in_threadpool(_collect, payload, window, session, db)
        except Exception:  # noqa: BLE001
            pass

        headers = _upstream_headers(request)
        headers.setdefault("anthropic-version", "2023-06-01")
        context = await run_in_threadpool(net.ssl_context)
        async with httpx.AsyncClient(timeout=timeout, verify=context) as client:
            try:
                response = await client.post(
                    _target("messages"), json=payload, headers=headers
                )
            except httpx.HTTPError as exc:
                return JSONResponse(
                    status_code=502, content={"error": {"message": f"上游请求失败：{exc}"}}
                )
        return JSONResponse(status_code=response.status_code, content=_safe_json(response))

    @router.get("/gateway/health")
    async def gateway_health() -> dict[str, Any]:
        return {
            "ok": True,
            "upstream": upstream_base_url,
            "inject_memory": service.settings.gateway_inject_memory,
            "default_db": default_db or service.settings.default_db,
            "note": "把聊天客户端的 base_url 指到本服务的 /v1 即可",
        }

    return router


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {"error": {"message": response.text[:500]}}


__all__ = ["MEMORY_BLOCK_HEADER", "build_memory_block", "create_gateway_router"]
