"""摘要生成：把对话切成"切片即记忆、同时是原文索引"的记忆单元。

设计文档 §4。三个关键决定：

1. **切片边界由摘要模型决定**，不按固定楼层。固定窗口会把 60 层的调试对话
   切成语义断裂的两段，也会把 5 层的关键决策稀释掉。
2. **original_text 与 l2_char 偏移由我们自己从 msg 区间推导，不采信模型给的字符数**。
   模型可以准确判断"哪几条消息属于这个记忆"，但让它数字符必然错。
   这是"区间指针准确性"验收标准能被满足的前提。
3. **入库前去重**（L0 的 SHA-256）。多库只把去重范围缩小到库内，不能替代去重。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from duramem import net
from duramem.config import Settings
from duramem.models import ChunkDraft, Message
from duramem.providers.embedding import EmbeddingProvider
from duramem.retrieval.vector_index import VectorIndex
from duramem.store.repository import Repository, content_hash
from duramem.text.tokenize import Segmenter, estimate_tokens

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class SummaryError(RuntimeError):
    pass


@dataclass
class SummaryOutcome:
    ok: bool
    added: int = 0
    duplicates: int = 0
    rejected: int = 0
    msg_id_start: int | None = None
    msg_id_end: int | None = None
    chunk_uids: list[str] = None  # type: ignore[assignment]
    warnings: list[str] = None  # type: ignore[assignment]
    model_used: str = ""
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.chunk_uids is None:
            self.chunk_uids = []
        if self.warnings is None:
            self.warnings = []

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "added": self.added,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "model_used": self.model_used,
            "duration_ms": self.duration_ms,
            "uids": self.chunk_uids,
        }
        if self.msg_id_start is not None:
            payload["msg_range"] = [self.msg_id_start, self.msg_id_end]
        if self.warnings:
            payload["warnings"] = self.warnings
        return payload


class SummaryProvider(Protocol):
    model: str

    def summarize(
        self, messages: Sequence[dict[str, Any]], max_chunks: int, settings: Settings
    ) -> list[dict[str, Any]]: ...


# ====================================================================== 提示词

SYSTEM_PROMPT = """你是一个记忆切片器。你的任务是把一段对话提炼成若干条"记忆切片"。

每条切片必须满足：
1. `summary_text`：对这段记忆的摘要，写成**完整的句子**，目标长度约 {target} 个 token，
   硬上限 {hard_limit} 个 token。宁短勿断——绝不要写出被截断的半句话。
2. `keywords`：3-8 个关键词，**必须包含**出现过的专有名词、项目代号、错误码、
   文件路径、变量名、人名。这些词对后续的关键词检索至关重要，不要遗漏。
3. `msg_id_start` / `msg_id_end`：这条记忆对应的原始消息 ID 区间（含两端）。
   必须严格取自输入消息里给出的 id，不要编造。区间应覆盖支撑该记忆的全部消息。

要求：
- 产出 1 到 {max_chunks} 条切片。宁可少而准，不要多而碎。
- 切片边界按**语义单元**划分（一次排查、一个决定、一个结论），
  不要按消息条数平均切分。
- 摘要必须保留这段对话里的**原子事实**：具体数字、错误码、文件路径、
  命令、以及"后来/最终"的结局（某方案最终被推翻还是被采纳、最终数值定了多少）。
  检索靠这些词召回，将来跨会话回答"X 最终怎么样了"也全靠它们——
  摘要里没有的事实等于没有发生过（P0 评测 Q16/Q20 的教训）。
- 不要编造输入里没有的信息。不确定的细节不要写进摘要。
- 如果这段对话没有任何值得长期记住的内容，返回空数组。

只输出 JSON，不要任何解释文字。格式：
{{"chunks": [{{"title": "简短标题", "summary_text": "摘要",
"keywords": ["词1", "词2"], "msg_id_start": 1, "msg_id_end": 4}}]}}"""


def _format_transcript(messages: Sequence[dict[str, Any]]) -> str:
    lines = []
    for msg in messages:
        role = msg.get("speaker") or msg.get("role") or "unknown"
        lines.append(f"[{msg['id']}] {role}: {msg.get('content', '')}")
    return "\n".join(lines)


def extract_json(text: str) -> Any:
    """从模型输出里抠出 JSON。容忍 ``` 包裹和前后废话。"""
    if not text:
        raise SummaryError("模型返回为空")
    stripped = text.strip()

    fence = _JSON_FENCE.search(stripped)
    if fence:
        stripped = fence.group(1).strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise SummaryError(f"无法解析模型返回的 JSON：{exc}") from exc
    raise SummaryError("模型返回中没有找到 JSON 对象")


class OpenAICompatibleSummarizer:
    """OpenAI 兼容 chat/completions，要求严格 JSON 输出。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0) -> None:
        if not base_url:
            raise SummaryError("缺少 SUMMARY_BASE_URL")
        if not api_key:
            raise SummaryError("缺少 SUMMARY_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._timeout = timeout

    def summarize(
        self, messages: Sequence[dict[str, Any]], max_chunks: int, settings: Settings
    ) -> list[dict[str, Any]]:
        system = SYSTEM_PROMPT.format(
            target=settings.summary_target_tokens,
            hard_limit=settings.summary_soft_limit_tokens,
            max_chunks=max_chunks,
        )
        try:
            response = net.shared_client().post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": _format_transcript(messages)},
                    ],
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # 同 embedding：传输层失败要归一成 SummaryError，
            # 否则调用方（REST / 网关）只能把它当未知错误
            raise SummaryError(f"连不上摘要接口 {self.base_url}：{exc}") from exc
        if response.status_code >= 400:
            raise SummaryError(f"摘要接口返回 {response.status_code}：{response.text[:300]}")
        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise SummaryError(f"摘要接口返回结构异常：{str(body)[:300]}") from exc

        parsed = extract_json(content)
        if isinstance(parsed, dict):
            chunks = parsed.get("chunks", [])
        elif isinstance(parsed, list):
            chunks = parsed
        else:
            chunks = []
        return [item for item in chunks if isinstance(item, dict)]


def probe_chat(
    base_url: str, api_key: str, model: str, timeout: float = 30.0
) -> str:
    """最小连通性探测：发一句极短的对话，返回模型的回话。

    刻意**不要求 JSON 输出**——探测的目的是验端点、密钥和模型名，
    不是验提示词。要求 JSON 会把"端点没问题但模型不爱守格式"误报成配置错误。
    """
    if not base_url:
        raise SummaryError("缺少 base_url")
    if not api_key:
        raise SummaryError("缺少 API Key")
    try:
        response = net.shared_client().post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "回复两个字：收到"}],
                "max_tokens": 16,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise SummaryError(f"连不上对话接口 {base_url}：{exc}") from exc
    if response.status_code >= 400:
        raise SummaryError(f"对话接口返回 {response.status_code}：{response.text[:300]}")
    try:
        return str(response.json()["choices"][0]["message"]["content"]).strip()[:100]
    except (KeyError, IndexError, TypeError) as exc:
        raise SummaryError(f"对话接口返回结构异常：{response.text[:300]}") from exc


class HeuristicSummarizer:
    """确定性离线摘要器。

    不是要替代真模型，而是让摘要→入库→检索→回查的整条链路在没有 API Key 的
    环境里可端到端测试。做法：把消息按语义密度分组，抽取式生成摘要。
    """

    model = "offline-heuristic"

    def __init__(self, segmenter: Segmenter, group_size: int = 4) -> None:
        self.segmenter = segmenter
        self.group_size = max(1, group_size)

    def summarize(
        self, messages: Sequence[dict[str, Any]], max_chunks: int, settings: Settings
    ) -> list[dict[str, Any]]:
        if not messages:
            return []
        out: list[dict[str, Any]] = []
        size = max(self.group_size, len(messages) // max_chunks or 1)
        for start in range(0, len(messages), size):
            group = list(messages[start : start + size])
            if not group:
                continue
            body = " ".join(str(m.get("content", "")) for m in group)
            tokens = self.segmenter.tokens(body)
            if not tokens:
                continue
            summary, _ = self._condense(body, settings.summary_soft_limit_tokens)
            keywords = []
            for token in tokens:
                if token not in keywords and (len(token) > 1 or not token.isascii()):
                    keywords.append(token)
                if len(keywords) >= 8:
                    break
            out.append(
                {
                    "title": summary[:24],
                    "summary_text": summary,
                    "keywords": keywords,
                    "msg_id_start": group[0]["id"],
                    "msg_id_end": group[-1]["id"],
                }
            )
            if len(out) >= max_chunks:
                break
        return out

    @staticmethod
    def _condense(text: str, max_tokens: int) -> tuple[str, bool]:
        from duramem.reader import truncate_to_tokens

        return truncate_to_tokens(text, max_tokens)


def build_summary_provider(
    settings: Settings, segmenter: Segmenter
) -> SummaryProvider:
    if settings.summary_api_key and settings.summary_base_url:
        return OpenAICompatibleSummarizer(
            settings.summary_base_url, settings.summary_api_key, settings.summary_model
        )
    return HeuristicSummarizer(segmenter)


# ====================================================================== 编排


class Summarizer:
    """摘要 → 校验 → 去重 → 嵌入 → 入库 → 推进游标。"""

    def __init__(
        self,
        repo: Repository,
        embedder: EmbeddingProvider,
        index: VectorIndex,
        settings: Settings,
        provider: SummaryProvider | None = None,
    ) -> None:
        self.repo = repo
        self.embedder = embedder
        self.index = index
        self.settings = settings
        self.provider = provider or build_summary_provider(settings, repo.segmenter)

    # ------------------------------------------------------------------ 触发

    def maybe_auto_summarize(self, window_id: str, session_id: str) -> SummaryOutcome | None:
        """固定楼层触发。默认关闭。"""
        if not self.settings.auto_summary_enabled:
            return None
        cursor = self.repo.get_cursor(window_id, session_id)
        latest = self.repo.max_message_id(window_id, session_id)
        pending = self.repo.message_count(window_id, session_id) - self._count_before(
            window_id, session_id, cursor
        )
        if pending < self.settings.summary_interval:
            return None
        if latest <= cursor:
            return None
        return self.summarize(window_id, session_id)

    def _count_before(self, window_id: str, session_id: str, cursor: int) -> int:
        row = self.repo.db.read_conn.execute(
            "SELECT count(*) AS c FROM messages WHERE window_id=? AND session_id=? AND id <= ?",
            (window_id, session_id, cursor),
        ).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------ 主流程

    def summarize(
        self,
        window_id: str,
        session_id: str,
        messages: Sequence[Message] | None = None,
        since_cursor: bool = True,
        source_db: str | None = None,
    ) -> SummaryOutcome:
        started = time.monotonic()
        warnings: list[str] = []

        if messages:
            self.repo.upsert_messages(messages)

        cursor = self.repo.get_cursor(window_id, session_id) if since_cursor else -1
        rows = self.repo.db.read_conn.execute(
            "SELECT * FROM messages WHERE window_id=? AND session_id=? AND id > ? ORDER BY id",
            (window_id, session_id, cursor),
        ).fetchall()
        all_rows = [dict(r) for r in rows]

        # 只把对话内容交给模型。宿主注入的提醒（56% 的 user 文本都可能是这类样板文）
        # 与助手在工具调用之间的叙述都不是知识，切进记忆只会污染检索。
        # 注意它们**仍在库里、仍在 L1 原文里**——read_original 依然能取到完整原文，
        # 这里只是不拿它们做摘要。
        transcript = [row for row in all_rows if row.get("role") != "system"]
        skipped_nonconversational = len(all_rows) - len(transcript)

        if len(transcript) < self.settings.summary_min_messages:
            return SummaryOutcome(
                ok=False,
                warnings=[
                    f"待总结消息不足（{len(transcript)} 条对话内容，最少 "
                    f"{self.settings.summary_min_messages} 条；"
                    f"另有 {skipped_nonconversational} 条非对话内容已跳过）"
                ],
                model_used=getattr(self.provider, "model", ""),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        msg_start, msg_end = transcript[0]["id"], transcript[-1]["id"]

        try:
            raw_chunks = self.provider.summarize(
                transcript, self.settings.summary_max_chunks, self.settings
            )
        except SummaryError as exc:
            self.repo.record_summary_run(
                window_id, session_id, msg_start, msg_end, 0, 0, 0,
                [str(exc)], getattr(self.provider, "model", ""),
                int((time.monotonic() - started) * 1000),
            )
            return SummaryOutcome(
                ok=False,
                warnings=[str(exc)],
                msg_id_start=msg_start,
                msg_id_end=msg_end,
                model_used=getattr(self.provider, "model", ""),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        valid_ids = {int(row["id"]) for row in transcript}
        drafts, duplicates, rejected = self._validate(
            raw_chunks, valid_ids, window_id, session_id, source_db, warnings
        )

        # 批量嵌入后入库
        added_uids: list[str] = []
        if drafts:
            texts = [
                " ".join(
                    [draft.summary_text, draft.title, *draft.keywords]
                ).strip()
                for draft in drafts
            ]
            try:
                vectors = self.embedder.embed(texts)
            except Exception as exc:  # noqa: BLE001 - 切片仍应入库，向量可后补
                warnings.append(f"嵌入失败，切片已入库但暂缺向量：{exc}")
                vectors = []

            # 单事务批量入库，链接收尾一次重建——逐条 insert_chunk 会把
            # 链接重建退化成 O(N²)（每条全量拉存活切片算交集）。
            results = self.repo.insert_chunks(
                drafts,
                source_db=source_db,
                summary_soft_limit=self.settings.summary_soft_limit_tokens,
            )
            added_uids = [result["uid"] for result in results]
            pending: list[tuple[int, list[float]]] = [
                (result["id"], vectors[position])
                for position, result in enumerate(results)
                if vectors and position < len(vectors)
            ]
            if pending:
                try:
                    self.index.add_many(pending)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"写入向量索引失败：{exc}")

        # 游标只推进到**实际被切片覆盖**的最后一条消息。
        # 直接推到 msg_end 是危险的：如果模型只覆盖了前半段（或返回了较短区间），
        # 后面的消息就永远不会再被总结——静默丢记忆。
        # 例外：模型明确返回空数组（判定这段没有值得记的内容）时才推到末尾，
        # 否则同一段对话会被反复重试。
        covered_end = max(
            (draft.msg_id_end for draft in drafts if draft.msg_id_end is not None),
            default=None,
        )
        # 模型判定"这段没有值得记的"时推到末尾。这里用**全部消息**的末尾而不是
        # 对话内容的末尾，否则结尾若有系统提醒，下一轮会把它再读一遍。
        batch_end = int(all_rows[-1]["id"]) if all_rows else int(msg_end)
        if covered_end is None and not drafts:
            cursor_target = batch_end
        elif covered_end is not None and covered_end > cursor:
            cursor_target = int(covered_end)
        else:
            cursor_target = None

        if cursor_target is not None:
            self.repo.set_cursor(window_id, session_id, cursor_target)
            if cursor_target < int(msg_end):
                warnings.append(
                    f"游标推进到消息 {cursor_target}（本次请求区间到 {msg_end}），"
                    "剩余消息会在下次总结时处理。"
                )

        duration = int((time.monotonic() - started) * 1000)
        self.repo.record_summary_run(
            window_id, session_id, msg_start, msg_end,
            len(added_uids), duplicates, rejected, warnings,
            getattr(self.provider, "model", ""), duration,
        )

        return SummaryOutcome(
            ok=True,
            added=len(added_uids),
            duplicates=duplicates,
            rejected=rejected,
            msg_id_start=msg_start,
            msg_id_end=msg_end,
            chunk_uids=added_uids,
            warnings=warnings,
            model_used=getattr(self.provider, "model", ""),
            duration_ms=duration,
        )

    # ------------------------------------------------------------------ 校验

    def _validate(
        self,
        raw_chunks: Sequence[dict[str, Any]],
        valid_ids: set[int],
        window_id: str,
        session_id: str,
        source_db: str | None,
        warnings: list[str],
    ) -> tuple[list[ChunkDraft], int, int]:
        drafts: list[ChunkDraft] = []
        duplicates = 0
        rejected = 0
        # 同一批次内部的去重：否则一次总结里模型吐出两条相同内容时会双双入库
        # （库内查重只能发现"之前批次"留下的重复）
        seen_in_batch: set[str] = set()

        for index, raw in enumerate(raw_chunks):
            if not isinstance(raw, dict):
                rejected += 1
                warnings.append(f"第 {index + 1} 条切片格式非法，已丢弃")
                continue

            summary = str(raw.get("summary_text") or "").strip()
            if not summary:
                rejected += 1
                warnings.append(f"第 {index + 1} 条切片缺少 summary_text，已丢弃")
                continue

            start = _as_int(raw.get("msg_id_start"))
            end = _as_int(raw.get("msg_id_end"))

            if start is None or end is None or start not in valid_ids or end not in valid_ids:
                rejected += 1
                warnings.append(
                    f"第 {index + 1} 条切片的 msg 区间越界或缺失"
                    f"（{raw.get('msg_id_start')}-{raw.get('msg_id_end')}），已丢弃"
                )
                continue
            if start > end:
                start, end = end, start

            # 区间指针由我们自己算——模型可以判断"哪几条消息"，但数字符必然错。
            # 这也是 L2 原文的唯一来源：模型不参与产出原文。
            original_text, offsets = self.repo.render_range(start, end)
            if not original_text:
                rejected += 1
                warnings.append(f"第 {index + 1} 条切片的消息区间为空，已丢弃")
                continue
            original_char_start, original_char_end = offsets[0][1], offsets[-1][2]

            model_l2 = str(raw.get("original_text") or "").strip()
            if model_l2 and model_l2 != original_text.strip():
                warnings.append(
                    f"第 {index + 1} 条切片：模型自述原文与消息区间渲染不一致，"
                    "已改用后者（区间指针以库内消息为准）"
                )

            digest = content_hash(summary)
            if digest in seen_in_batch:
                duplicates += 1
                warnings.append(f"第 {index + 1} 条切片与本批次内已有切片内容重复，已跳过")
                continue
            if self.repo.find_alive_by_hash(digest) is not None:
                duplicates += 1
                continue
            seen_in_batch.add(digest)

            keywords = [str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip()]
            summary_tokens = estimate_tokens(summary)
            if summary_tokens > self.settings.summary_soft_limit_tokens:
                warnings.append(
                    f"第 {index + 1} 条切片 摘要超软上限（{summary_tokens} > "
                    f"{self.settings.summary_soft_limit_tokens} token）"
                )

            drafts.append(
                ChunkDraft(
                    summary_text=summary,
                    original_text=original_text,
                    title=str(raw.get("title") or "").strip(),
                    keywords=keywords[:12],
                    msg_id_start=start,
                    msg_id_end=end,
                    original_char_start=original_char_start,
                    original_char_end=original_char_end,
                    source_window=window_id,
                    source_session=session_id,
                )
            )

        return drafts, duplicates, rejected


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "HeuristicSummarizer",
    "OpenAICompatibleSummarizer",
    "Summarizer",
    "SummaryError",
    "SummaryOutcome",
    "SummaryProvider",
    "build_summary_provider",
    "extract_json",
]
