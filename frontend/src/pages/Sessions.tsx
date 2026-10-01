import { useCallback, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { api, formatTime } from '../api'
import type { SessionLayer, SessionRefreshResponse } from '../types'
import { Badge, Button, Card, Empty, ErrorBox, NoticeBox, Spinner, Stat } from '../components/ui'
import { IconCpu, IconHash, IconLayers, IconLink, IconMessages, IconRefresh, IconSparkles } from '../components/icons'

/**
 * 会话页：按**会话**（容器）看记忆，而不是按切片（条目）。
 *
 * 三层结构里概览挂在会话上、切片上没有——所以「这段会话整体在讲什么」
 * 这个问题在这一页回答，切片浏览器回答的是「某条记忆是什么」。
 *
 * 概览里有一段「导航」，用「想知道什么 → uid」指路。模型据此决定去读哪条
 * 切片的原文。这一页把它摆出来，人也一样能按它跳。
 */
export default function Sessions({ db, onOpenChunk }: { db: string; onOpenChunk: (uid: string) => void }) {
  const [items, setItems] = useState<SessionLayer[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.sessionLayers(db)
      setItems(res.sessions)
      setSelected((current) =>
        current && res.sessions.some((s) => s.session_id === current) ? current : (res.sessions[0]?.session_id ?? null),
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [db])

  useEffect(() => {
    void load()
  }, [load])

  const refresh = async (force: boolean, sessionId?: string) => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const res: SessionRefreshResponse = await api.refreshSessionLayers(db, { session_id: sessionId, force })
      const skipped = res.results.filter((r) => r.ok === null)
      setNotice(
        `${res.refreshed} 个已重生成` + (skipped.length ? `；${skipped.length} 个按策略跳过（${skipped[0].reason}）` : ''),
      )
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const current = items.find((s) => s.session_id === selected) ?? null

  return (
    <div className="space-y-4">
      {error && <ErrorBox onClose={() => setError(null)}>{error}</ErrorBox>}
      {notice && <NoticeBox onClose={() => setNotice(null)}>{notice}</NoticeBox>}

      <Card
        icon={IconMessages}
        title="会话概览"
        subtitle="容器层：一段会话的整体脉络与导航。切片层只有摘要与原文，概览挂在这里。"
        bodyClassName="p-0"
        right={
          <>
            <Button size="sm" icon={IconRefresh} onClick={() => void refresh(false)} disabled={busy}>
              按策略刷新
            </Button>
            <Button
              size="sm"
              icon={IconSparkles}
              onClick={() => void refresh(true, selected ?? undefined)}
              disabled={busy || !selected}
              title="绕过 freshness 策略，立刻重生成选中的这一个"
            >
              重新生成选中
            </Button>
          </>
        }
      >
        {loading ? (
          <div className="flex items-center justify-center gap-2 py-12 text-xs text-slate-400">
            <Spinner /> 载入中…
          </div>
        ) : items.length === 0 ? (
          <Empty icon={IconMessages}>
            还没有会话概览。概览不会自动生成——采集 hook 是同步执行的，在里面塞模型调用会拖慢每一轮对话。点「按策略刷新」为已有切片生成。
          </Empty>
        ) : (
          <div className="grid grid-cols-1 divide-y divide-white/[0.06] xl:grid-cols-[minmax(0,22rem)_minmax(0,1fr)] xl:divide-x xl:divide-y-0">
            <div className="max-h-[38rem] space-y-1 overflow-y-auto p-2.5">
              {items.map((item) => {
                const active = item.session_id === selected
                return (
                  <button
                    key={`${item.window_id}/${item.session_id}`}
                    type="button"
                    aria-current={active}
                    onClick={() => setSelected(item.session_id)}
                    className={`w-full rounded-xl border px-3 py-2.5 text-left ${
                      active
                        ? 'border-sky-400/30 bg-sky-400/[0.07]'
                        : 'border-transparent hover:border-white/[0.06] hover:bg-white/[0.02]'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-2xs text-slate-400 mono">{item.session_id}</span>
                      {item.pending_changes > 0 && <Badge tone="amber">有 {item.pending_changes} 处待跟上</Badge>}
                    </div>
                    <div className={`mt-1 line-clamp-3 text-xs leading-relaxed ${active ? 'text-slate-200' : 'text-slate-400'}`}>
                      {item.abstract_text || '（无简介）'}
                    </div>
                  </button>
                )
              })}
            </div>

            {current ? (
              <div className="min-w-0 space-y-3.5 p-4">
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <Stat icon={IconLayers} label="覆盖切片" value={`${current.coverage_total} 条`} />
                  <Stat
                    icon={IconHash}
                    label="实际送入"
                    value={current.coverage_sampled === current.coverage_total ? '全量' : `${current.coverage_sampled} 条`}
                    hint="超过抽样上限时按区间均匀抽样"
                  />
                  <Stat icon={IconMessages} label="概览长度" value={`${current.overview_tokens} tok`} />
                  <Stat icon={IconCpu} label="生成模型" value={current.model_used ?? '—'} />
                </div>
                <div className="text-2xs text-slate-500">
                  窗口 <span className="text-slate-400 mono">{current.window_id}</span> · 更新于 {formatTime(current.updated_at)}
                </div>
                <div className="dm-panel max-h-[28rem] overflow-y-auto px-4 py-3.5 text-xs">
                  <div className="markdown-body">
                    <ReactMarkdown>{current.overview_text}</ReactMarkdown>
                  </div>
                </div>
                <Navigation overview={current.overview_text} onOpenChunk={onOpenChunk} />
              </div>
            ) : (
              <Empty compact>左侧选一个会话。</Empty>
            )}
          </div>
        )}
      </Card>
    </div>
  )
}

/**
 * 把概览「导航」段里的 uid 抽出来做成可点的跳转。
 *
 * 为什么值得单独做：导航段是概览相对简介的**核心增量**——它告诉模型（和人）
 * 「相关的话该去看哪一条」。摆成可点的，这个价值才落得下来。
 */
function Navigation({ overview, onOpenChunk }: { overview: string; onOpenChunk: (uid: string) => void }) {
  const uids = Array.from(new Set(overview.match(/uid[:：]\s*([0-9A-Za-z]{4,})/g) ?? [])).map((line) =>
    line.replace(/^uid[:：]\s*/, ''),
  )
  if (uids.length === 0) return null
  return (
    <div className="space-y-2">
      <div className="text-2xs font-medium text-slate-300">
        导航指向的切片 <span className="font-normal text-slate-500">（概览里出现的 uid，点开直达）</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {uids.map((uid) => (
          <button key={uid} type="button" title="在切片浏览器里打开" onClick={() => onOpenChunk(uid)} className="dm-chip mono">
            <IconLink size={12} />
            {uid}
          </button>
        ))}
      </div>
    </div>
  )
}
