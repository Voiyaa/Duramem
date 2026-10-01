import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, shortDate } from '../api'
import type { Chunk, Session, SummaryRun } from '../types'
import { Card, Empty, ErrorBox, NoticeBox, Stat } from '../components/ui'
import {
  IconActivity,
  IconChart,
  IconCheckCircle,
  IconLayers,
  IconMessages,
  IconTrending,
} from '../components/icons'
import DailyChart, { ChartLegend, type DailyPoint } from './timeline/DailyChart'
import { HotList, RunsCard, SummarizeCard } from './timeline/Lists'

export default function Timeline({ db }: { db: string }) {
  const [chunks, setChunks] = useState<Chunk[]>([])
  const [sessions, setSessions] = useState<Session[]>([])
  const [runs, setRuns] = useState<SummaryRun[]>([])
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const reload = useCallback(async () => {
    if (!db) return
    try {
      const [chunkResult, sessionResult, runResult] = await Promise.all([
        api.listChunks({ db, include_deleted: true, limit: 2000 }),
        api.sessions(db),
        api.summaryRuns(db, 100),
      ])
      setChunks(chunkResult.chunks)
      setSessions(sessionResult.sessions)
      setRuns(runResult.runs)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [db])

  useEffect(() => {
    void reload()
  }, [reload])

  const daily = useMemo(() => {
    const buckets = new Map<string, DailyPoint>()
    for (const chunk of chunks) {
      const key = chunk.created_at.slice(0, 10)
      const entry = buckets.get(key) ?? { date: shortDate(chunk.created_at), 新增: 0, 命中: 0 }
      entry.新增 += 1
      entry.命中 += chunk.hit_count
      buckets.set(key, entry)
    }
    return [...buckets.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([, v]) => v)
  }, [chunks])

  const hot = useMemo(() => [...chunks].sort((a, b) => b.hit_count - a.hit_count).slice(0, 12), [chunks])
  const summarizable = useMemo(() => sessions.filter((session) => session.messages > 0), [sessions])

  const runSummarize = async (session: Session) => {
    setBusy(`${session.window_id}/${session.session_id}`)
    setError(null)
    setNotice(null)
    try {
      const result = await api.summarize(session.window_id, session.session_id, db)
      const detail = `新增 ${result.added} 条，去重 ${result.duplicates}，丢弃 ${result.rejected}`
      setNotice(
        result.ok
          ? { ok: true, text: `总结完成：${detail}` }
          : { ok: false, text: `未生成切片：${(result.warnings ?? []).join('；')}` },
      )
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  if (!db) return <Empty icon={IconActivity}>先在「库管理」里选一个库。</Empty>

  const totalHits = chunks.reduce((sum, chunk) => sum + chunk.hit_count, 0)

  return (
    <div className="space-y-4">
      {error && <ErrorBox onClose={() => setError(null)}>{error}</ErrorBox>}
      {notice && (
        <NoticeBox tone={notice.ok ? 'success' : 'warn'} onClose={() => setNotice(null)}>
          {notice.text}
        </NoticeBox>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat icon={IconLayers} label="切片总数" value={chunks.length} hint="含已删除与已被取代" />
        <Stat icon={IconTrending} label="累计命中" value={totalHits} hint="每被检索命中一次 +1" />
        <Stat icon={IconMessages} label="会话数" value={sessions.length} />
        <Stat icon={IconCheckCircle} label="总结次数" value={runs.length} />
      </div>

      <Card icon={IconChart} title="切片新增与命中分布" subtitle="按创建日期聚合" right={<ChartLegend />}>
        {daily.length === 0 ? (
          <Empty icon={IconChart}>还没有切片，没有可画的东西。</Empty>
        ) : (
          <DailyChart data={daily} />
        )}
      </Card>

      <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
        <HotList chunks={hot} />
        <div className="space-y-4">
          <SummarizeCard
            sessions={summarizable}
            runs={runs}
            busy={busy}
            onSummarize={(session) => void runSummarize(session)}
          />
          <RunsCard runs={runs} />
        </div>
      </div>
    </div>
  )
}
