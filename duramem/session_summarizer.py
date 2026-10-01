"""会话层生成：把一批切片聚合成一份会话概览（L1），再派生出会话摘要（L0）。

仿 OpenViking 的两级容器 sidecar：

| | 来源 | 长度 | 作用 |
|---|---|---|---|
| L1 概览 | 模型生成 | ≤ overview_target_tokens（1600） | 定位、导航、精排打分体 |
| L0 摘要 | **从 L1 首段机械抽取** | ≤ abstract_tokens（256） | 进检索索引、便宜地判相关性 |

**一次模型调用产出两层。** L0 不是第二次生成——它是 L1 正文里第一个二级标题
之前那段。这样两层永不互相矛盾，也没有额外成本（照 OpenViking 的
`_extract_abstract_from_overview`）。

L1 的结构是**地图，不是缩印本**：标题 / 简述 / 覆盖度 / 导航，每条导航行内联
一句话结论。曾经还有第五节「详细说明」（逐切片重述），已删——L1 本身就是
由切片 L0 聚合生成的，详述的信息量 ⊆ 各切片 L0，是派生冗余；切片内容走
L0（`dm_search` 命中、或 `dm_read_neighbors(uid, before=0, after=0)` 按
导航 uid 跟随），逐字细节走 L2（`dm_read_original`）。L1 相对 L0 的核心
增量只有三样：跨切片的叙事主线、覆盖度盲区、uid 指路。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from duramem import net
from duramem.config import Settings
from duramem.retrieval.vector_index import VectorIndex
from duramem.store.session_layers import SessionLayerDraft, SessionLayerStore
from duramem.text.tokenize import Segmenter, estimate_tokens

_FRONTMATTER_FENCE = "---"
_H1_RE = re.compile(r"^#\s+")
_H2_RE = re.compile(r"^##\s+")

SESSION_OVERVIEW_PROMPT = """你是一个会话概览器。输入是一段会话里已有的若干条记忆切片，\
每条带 uid、标题、摘要与消息区间。

你的任务是写出一份**会话概览**，让后续的模型不必读原文就能知道：这段会话讲了什么，以及需要细节时该去看哪一条切片。

它是**地图，不是缩印本**：你的价值是跨切片的叙事线、覆盖度盲区与 uid 指路，\
不是重述切片内容——每条切片的摘要在输入里已经给出，重复它们没有价值。

严格按下面的结构输出 Markdown，不要有开场白或额外解释：

# <会话标题>

<一段连贯的简述，约 300-500 token，覆盖这段会话的主线（切片之间如何衔接、\
演化到什么结局）。这一段会被单独抽出来当作会话摘要，所以它必须自成一体，\
不要依赖下面的内容。>

## 覆盖度

<说明这份概览基于多少条切片、是否覆盖了整个会话。输入若是抽样，要明说。>

## 导航

<用「想知道什么 → uid: xxx（一句话结论）」的形式给出决策树，每行一条，\
结论要短、必须是那条切片真正的要点，例如：
- 想知道数据库选型 → uid: a1b2c3d4e5f6（结论：选 sqlite-vec，同库单文件）
- 想知道 bat 脚本的坑 → uid: 0f9e8d7c6b5a（结论：GBK 无 BOM，延时用 ping -n） >

硬性要求：
- 总长度不超过 {hard_limit} token。
- 只写输入里有的信息，不要推测、不要补充外部知识。
- 导航段里的 uid 必须逐字来自输入，不许编造。
- 只输出 Markdown 正文，不要 JSON、不要代码围栏。"""


def sample_slices(slices: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """按区间**均匀**抽样，且保证首尾都在。

    为什么不能取前 N 条：概览要覆盖整个会话，只取开头会让后半段整个消失，
    而那里恰恰是导航最该指向的地方（最近的结论就在尾部）。
    这里用跨 [0, n-1] 闭区间的等距下标，所以第一条与最后一条一定入选。
    """
    rows = list(slices)
    if limit <= 0 or len(rows) <= limit:
        return rows
    if limit == 1:
        # 只留一条时留**最后**一条：问"我们刚才在做什么"时它信息量最大
        return [rows[-1]]
    step = (len(rows) - 1) / (limit - 1)
    indices = sorted({round(i * step) for i in range(limit)})
    return [rows[i] for i in indices]


def abstract_from_overview(overview_text: str, max_tokens: int) -> str:
    """从 L1 正文里机械抽出 L0（会话摘要）。

    照 OpenViking 的 `_extract_abstract_from_overview`：跳过开头的 YAML
    frontmatter 与一级标题，取到第一个二级标题之前的那段正文，再按句末截断。

    取不到正文时退回整段 L1 的开头——**不能返回空**：L0 是会话进词法索引的
    唯一来源，空 L0 会让这个会话在关键词检索里彻底消失。
    """
    body: list[str] = []
    in_frontmatter = False
    seen_h1 = False
    for raw in (overview_text or "").splitlines():
        line = raw.strip()
        if not seen_h1 and line == _FRONTMATTER_FENCE:
            # 只认开头处的围栏；正文里的 --- 是水平分隔线
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if not seen_h1 and _H1_RE.match(line):
            seen_h1 = True
            continue
        if _H2_RE.match(line):
            break
        if line:
            body.append(line)

    text = " ".join(body).strip() or (overview_text or "").strip()
    truncated, _ = _truncate(text, max_tokens)
    return truncated.strip()


def _truncate(text: str, max_tokens: int) -> tuple[str, bool]:
    # 延迟导入：`reader` 会连带拉进 repository / 模型层，而那边不需要知道
    # 生成侧的存在。与 HeuristicSummarizer 的做法一致。
    from duramem.reader import truncate_to_tokens

    return truncate_to_tokens(text, max_tokens)


# ====================================================================== 提供方


class OverviewProvider(Protocol):
    """生成会话概览的提供方。两个实现共用这一个签名。"""

    model: str

    def overview(
        self,
        slices: Sequence[dict[str, Any]],
        session_id: str,
        settings: Settings,
    ) -> str: ...


def _format_slices(slices: Sequence[dict[str, Any]]) -> str:
    lines = []
    for row in slices:
        span = f"{row.get('msg_id_start')}-{row.get('msg_id_end')}"
        lines.append(
            f"uid: {row.get('chunk_uid')}\n"
            f"标题: {row.get('title') or '(无)'}\n"
            f"消息区间: {span}\n"
            f"摘要: {row.get('summary_text') or ''}"
        )
    return "\n\n".join(lines)


class OpenAICompatibleOverviewProvider:
    """OpenAI 兼容 chat/completions，产出 Markdown 概览。

    刻意**不要 JSON**：概览正文就是 L1 内容本身，不是结构化数据。要求 JSON
    只会多一层解析失败面，而 `extract_json` 那套容错在长文本上更容易出岔子。
    """

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 180.0) -> None:
        if not base_url:
            raise OverviewError("缺少 SUMMARY_BASE_URL")
        if not api_key:
            raise OverviewError("缺少 SUMMARY_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._timeout = timeout

    def overview(
        self,
        slices: Sequence[dict[str, Any]],
        session_id: str,
        settings: Settings,
    ) -> str:
        system = SESSION_OVERVIEW_PROMPT.format(
            hard_limit=settings.overview_target_tokens,
        )
        user = (
            f"会话标识：{session_id}\n切片总数：{len(slices)}\n\n{_format_slices(slices)}"
        )
        try:
            response = net.shared_client().post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.2,
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise OverviewError(f"连不上摘要接口 {self.base_url}：{exc}") from exc
        if response.status_code >= 400:
            raise OverviewError(
                f"摘要接口返回 {response.status_code}：{response.text[:300]}"
            )
        body = response.json()
        try:
            return str(body["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError) as exc:
            raise OverviewError(f"摘要接口返回结构异常：{str(body)[:300]}") from exc


class HeuristicOverviewProvider:
    """确定性离线概览器。

    不是要替代真模型，而是让"切片 → 概览 → 派生 L0 → 落库 → 被检索到"整条
    链路在没有 API Key 的环境里可端到端测试。产出同样的结构（标题 / 简述 /
    覆盖度 / 导航，无详述节），所以 `abstract_from_overview` 的抽取逻辑被真实覆盖。
    """

    model = "offline-heuristic"

    def __init__(self, segmenter: Segmenter) -> None:
        self.segmenter = segmenter

    def overview(
        self,
        slices: Sequence[dict[str, Any]],
        session_id: str,
        settings: Settings,
    ) -> str:
        rows = list(slices)
        lead = " ".join(str(r.get("summary_text") or "") for r in rows)
        lead_truncated, _ = _truncate(lead.strip() or "（本会话暂无切片内容）", 200)

        lines = [f"# 会话 {session_id}", "", lead_truncated, "", "## 覆盖度", ""]
        lines.append(f"本概览基于 {len(rows)} 条切片生成，覆盖整个会话，未抽样。")
        lines += ["", "## 导航", ""]
        for row in rows:
            title = str(row.get("title") or "").strip() or "（无标题）"
            lines.append(f"- 想知道{title} → uid: {row.get('chunk_uid')}")
        return "\n".join(lines).strip()


class OverviewError(RuntimeError):
    """概览生成失败。"""


def build_overview_provider(settings: Settings, segmenter: Segmenter) -> OverviewProvider:
    if settings.summary_api_key and settings.summary_base_url:
        return OpenAICompatibleOverviewProvider(
            settings.summary_base_url, settings.summary_api_key, settings.summary_model
        )
    return HeuristicOverviewProvider(segmenter)


# ====================================================================== 编排


@dataclass
class SessionSummaryOutcome:
    ok: bool
    window_id: str = ""
    session_id: str = ""
    model_used: str | None = None
    overview_tokens: int = 0
    abstract_tokens: int = 0
    coverage_total: int = 0
    coverage_sampled: int = 0
    warnings: list[str] = field(default_factory=list)


# ====================================================================== 刷新策略

NOOP = "noop"
MARK_PENDING = "mark_pending"
REFRESH_NOW = "refresh_now"


def decide_refresh(
    *,
    has_layer: bool,
    slice_count: int,
    pending: int,
    sample_limit: int,
    refresh_ratio: float,
) -> tuple[str, str]:
    """决定要不要重新生成会话概览。返回 (决策, 理由)。

    `slice_count` 是**当前**切片数（不是上次生成时记的 `coverage_total`）——
    比例阈值要衡量的是"在现在这么宽的容器里，有多少处变了"。

    `sample_limit <= 0` 表示不限抽样，此时"小容器立即刷新"这一条不适用，
    一切由比例决定。

    照 OpenViking 的 `decide_parent_refresh()` 决策表：

    | 条件 | 决策 |
    |---|---|
    | 还没有概览 | REFRESH_NOW |
    | 没有待跟上的变化 | NOOP |
    | 切片数不超过抽样上限 | REFRESH_NOW |
    | 待跟上占比 >= 阈值 | REFRESH_NOW |
    | 否则 | MARK_PENDING |

    **"还没有概览"必须排在"没有待跟上的变化"之前**，顺序不能反：没概览的会话
    必然也没有 pending 计数（计数是挂在概览行上的），先判 pending 会让首次生成
    永远走不到。这个顺序是实测抓出来的。

    "切片数不超过抽样上限就立即刷新"这条是刻意的：那类会话的概览本就覆盖全部
    切片，重生成成本低，攒着反而让概览长期落后。宽会话才需要靠比例摊薄成本。

    **不做时间兜底**（与 OpenViking 一致地不做）：宽会话在比例阈值下可能长期
    不刷新——161 条切片只变了 3 条就永远够不到 10%。这是明知而接受的取舍：
    加定时器会让"什么时候花钱"变得不可预测，而且它掩盖的是"其实不够重要"。
    想立刻更新就显式重生成。

    返回理由而不只是布尔，是因为"为什么没刷新"必须能被解释——否则用户只看到
    概览没更新，无从判断是策略拦截还是出了故障。
    """
    if not has_layer:
        return REFRESH_NOW, "该会话还没有概览"
    if pending <= 0:
        return NOOP, "没有待跟上的变化"
    if sample_limit > 0 and slice_count <= sample_limit:
        return (
            REFRESH_NOW,
            f"切片数 {slice_count} 不超过抽样上限 {sample_limit}，重生成成本低",
        )
    ratio = pending / max(slice_count, 1)
    if ratio >= refresh_ratio:
        return (
            REFRESH_NOW,
            f"待跟上变化 {pending}/{slice_count} = {ratio:.0%} ≥ {refresh_ratio:.0%}",
        )
    return (
        MARK_PENDING,
        f"待跟上变化 {pending}/{slice_count} = {ratio:.0%} < {refresh_ratio:.0%}，先记着",
    )


@dataclass
class RefreshOutcome:
    """一次 freshness 判断的结果。

    `summary` 为空表示**没有生成**（NOOP / MARK_PENDING）——那不是错误，
    所以不能塞进 `SessionSummaryOutcome.ok=False` 里表达，否则调用方会把
    "策略说先别动"误报成失败。
    """

    decision: str
    reason: str
    summary: SessionSummaryOutcome | None = None


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def session_embedding_text(row: dict[str, Any]) -> str:
    """会话层的嵌入输入 = **只有简介正文**。

    刻意不加 window_id / session_id：那些是标识不是语义，进了向量空间只会
    引入噪声。这一点与 OpenViking 一致——它的目录嵌入元数据白名单里只有
    `directory` 一个字段，时间戳与生成者都被排除。
    """
    return str(row.get("abstract_text") or "").strip()


class SessionSummarizer:
    """切片摘要 → 会话概览 → 派生简介 → 落库 → 嵌入简介。"""

    def __init__(
        self,
        store: SessionLayerStore,
        provider: OverviewProvider,
        settings: Settings,
        embedder: Embedder,
        index: VectorIndex,
    ) -> None:
        self.store = store
        self.provider = provider
        self.settings = settings
        self.embedder = embedder
        self.index = index

    def maybe_refresh(self, window_id: str, session_id: str) -> RefreshOutcome:
        """按 freshness 策略决定是否重生成。"""
        layer = self.store.get(window_id, session_id)
        pending = int(layer["pending_changes"]) if layer is not None else 0
        slice_count = self.store.count_slices(window_id, session_id)
        decision, reason = decide_refresh(
            has_layer=layer is not None,
            slice_count=slice_count,
            pending=pending,
            sample_limit=self.settings.session_overview_sample_limit,
            refresh_ratio=self.settings.session_overview_refresh_ratio,
        )
        if decision != REFRESH_NOW:
            return RefreshOutcome(decision=decision, reason=reason)
        return RefreshOutcome(
            decision=decision,
            reason=reason,
            summary=self.summarize(window_id, session_id),
        )

    def summarize(self, window_id: str, session_id: str) -> SessionSummaryOutcome:
        """强制生成/重生成该会话的概览。不判断是否该刷新——那是调用方的事。"""
        warnings: list[str] = []
        all_slices = self.store.slices_of_session(window_id, session_id)
        if not all_slices:
            return SessionSummaryOutcome(
                ok=False,
                window_id=window_id,
                session_id=session_id,
                warnings=["该会话还没有切片，无法生成概览。"],
            )

        sampled = sample_slices(all_slices, self.settings.session_overview_sample_limit)
        if len(sampled) < len(all_slices):
            warnings.append(
                f"切片数 {len(all_slices)} 超过抽样上限 "
                f"{self.settings.session_overview_sample_limit}，"
                "已按区间均匀抽样（首尾都保留）。"
            )

        try:
            overview = self.provider.overview(sampled, session_id, self.settings)
        except OverviewError as exc:
            return SessionSummaryOutcome(
                ok=False,
                window_id=window_id,
                session_id=session_id,
                warnings=[f"概览生成失败：{exc}"],
            )

        overview = (overview or "").strip()
        if not overview:
            return SessionSummaryOutcome(
                ok=False,
                window_id=window_id,
                session_id=session_id,
                warnings=["概览生成为空，未写入。"],
            )

        overview_tokens = estimate_tokens(overview)
        if overview_tokens > self.settings.overview_target_tokens:
            warnings.append(
                f"概览长度 {overview_tokens} token 超过上限 "
                f"{self.settings.overview_target_tokens}（仅提示，已照常写入）。"
            )

        abstract = abstract_from_overview(overview, self.settings.abstract_tokens)
        if not abstract:
            warnings.append("未能从概览里抽出摘要段，会话 L0 为空。")

        row = self.store.upsert(
            SessionLayerDraft(
                window_id=window_id,
                session_id=session_id,
                overview_text=overview,
                abstract_text=abstract,
                model_used=getattr(self.provider, "model", None),
                coverage_total=len(all_slices),
                coverage_sampled=len(sampled),
            )
        )

        # 嵌入 L0 让"我们接着上次那个来"这类没有关键词的查询能命中会话本身。
        # 失败不让整次生成失败——概览已经落库了，缺向量只是少一条召回路。
        if row and row.get("abstract_text"):
            try:
                vector = self.embedder.embed([session_embedding_text(row)])[0]
                self.index.add(int(row["id"]), vector)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"会话概览已写入，但向量未生成：{exc}")
        return SessionSummaryOutcome(
            ok=True,
            window_id=window_id,
            session_id=session_id,
            model_used=getattr(self.provider, "model", None),
            overview_tokens=int(row.get("overview_tokens") or overview_tokens),
            abstract_tokens=int(row.get("abstract_tokens") or 0),
            coverage_total=len(all_slices),
            coverage_sampled=len(sampled),
            warnings=warnings,
        )
