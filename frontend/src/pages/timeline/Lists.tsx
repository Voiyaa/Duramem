import { formatTime, parseList } from '../../api'
import type { Chunk, Session, SummaryRun } from '../../types'
import { Badge, Button, Card, Empty, Spinner } from '../../components/ui'
import { IconClock, IconPlay, IconTrending } from '../../components/icons'

export function HotList({ chunks }: { chunks: Chunk[] }) {
  return (
    <Card
      icon={IconTrending}
      title="高频命中的记忆"
      subtitle="命中次数只用于展示，不参与排序（否则会形成热门越热门的回音室）"
      bodyClassName="p-0"
    >
      {chunks.length === 0 ? (
        <Empty compact>还没有命中记录。</Empty>
      ) : (
        <ol className="divide-y divide-white/[0.05]">
          {chunks.map((chunk, index) => (
            <li key={chunk.chunk_uid} className="flex items-center gap-3 px-4 py-2.5">
              <span
                className={`grid size-6 shrink-0 place-items-center rounded-md text-2xs font-semibold tabular-nums ${
                  index < 3
                    ? 'bg-linear-to-b from-sky-400/25 to-violet-500/20 text-sky-100 ring-1 ring-sky-400/30 ring-inset'
                    : 'bg-slate-800/60 text-slate-500'
                }`}
              >
                {index + 1}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 items-center gap-1.5">
                  <span className="truncate text-xs font-medium text-slate-100">{chunk.title || '（无标题）'}</span>
                  {chunk.deleted_at && <Badge tone="rose">已删除</Badge>}
                  {chunk.superseded_by && <Badge tone="amber">已被取代</Badge>}
                </div>
                <div className="mt-0.5 line-clamp-1 text-2xs text-slate-500">{chunk.summary_text}</div>
              </div>
              <span
                title="命中次数"
                className="shrink-0 rounded-full bg-sky-400/10 px-2 py-0.5 text-2xs font-semibold text-sky-300 tabular-nums"
              >
                {chunk.hit_count}
              </span>
            </li>
          ))}
        </ol>
      )}
    </Card>
  )
}

export function SummarizeCard({
  sessions,
  runs,
  busy,
  onSummarize,
}: {
  sessions: Session[]
  runs: SummaryRun[]
  busy: string | null
  onSummarize: (session: Session) => void
}) {
  return (
    <Card
      icon={IconPlay}
      title="会话与总结"
      subtitle="总结受游标控制，两个窗口共用一库时不会重复总结同一段"
      bodyClassName="p-0"
    >
      {sessions.length === 0 ? (
        <Empty compact>
          还没有采集到消息。用 <code>duramem ingest</code> 或记忆网关喂数据。
        </Empty>
      ) : (
        <div className="divide-y divide-white/[0.05]">
          {sessions.map((session) => {
            const key = `${session.window_id}/${session.session_id}`
            const last = runs.find(
              (run) => run.window_id === session.window_id && run.session_id === session.session_id,
            )
            return (
              <div key={key} className="flex items-center gap-3 px-4 py-2.5">
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs text-slate-200 mono">{key}</div>
                  <div className="mt-0.5 text-2xs text-slate-500 tabular-nums">
                    {session.messages} 条消息 · 最新 #{session.last_msg_id}
                    {last && ` · 已总结到 #${last.msg_id_end}`}
                  </div>
                </div>
                <Button
                  size="sm"
                  icon={busy === key ? undefined : IconPlay}
                  disabled={busy !== null}
                  onClick={() => onSummarize(session)}
                  title="对游标之后的新消息触发一次总结"
                >
                  {busy === key && <Spinner label="总结中" />}
                  总结
                </Button>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}

export function RunsCard({ runs }: { runs: SummaryRun[] }) {
  return (
    <Card icon={IconClock} title="最近总结记录" bodyClassName="p-0">
      {runs.length === 0 ? (
        <Empty compact>还没有总结记录。</Empty>
      ) : (
        <div className="max-h-72 divide-y divide-white/[0.05] overflow-y-auto">
          {runs.slice(0, 20).map((run) => {
            const warnings = parseList(run.warnings)
            return (
              <div key={run.id} className="px-4 py-2.5">
                <div className="flex items-center gap-2 text-2xs tabular-nums">
                  <span className="font-medium text-slate-200">
                    #{run.msg_id_start}–{run.msg_id_end}
                  </span>
                  <span className="rounded bg-emerald-400/10 px-1.5 text-emerald-300">+{run.chunks_added}</span>
                  {run.duplicates > 0 && <span className="text-slate-500">去重 {run.duplicates}</span>}
                  {run.rejected > 0 && <span className="text-amber-300">丢弃 {run.rejected}</span>}
                  <span className="ml-auto text-slate-500">{run.duration_ms}ms</span>
                </div>
                <div className="mt-0.5 text-2xs text-slate-500">
                  {formatTime(run.created_at)} · {run.model_used}
                </div>
                {warnings.length > 0 && (
                  <div className="mt-0.5 line-clamp-2 text-2xs text-amber-300/80">{warnings.join('；')}</div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}
