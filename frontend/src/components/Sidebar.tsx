import type { Health } from '../types'
import { NAV, type PageKey } from '../routes'
import { Spinner } from './data'
import { LogoMark } from './icons'

/** lg 以下收成 64px 的图标栏：文字隐藏，悬停 title 给出完整名称。 */
export default function Sidebar({
  page,
  onNavigate,
  health,
  loading,
  fatal,
}: {
  page: PageKey
  onNavigate: (page: PageKey) => void
  health: Health | null
  loading: boolean
  fatal: string | null
}) {
  return (
    <aside className="relative z-10 flex w-16 shrink-0 flex-col border-r border-white/[0.06] bg-ink-950/70 backdrop-blur-xl lg:w-56">
      <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-white/[0.06] px-4 max-lg:justify-center max-lg:px-0">
        <span className="grid size-8 shrink-0 place-items-center rounded-[10px] bg-linear-to-b from-slate-800 to-slate-950 shadow-[0_6px_18px_-6px_rgba(14,165,233,0.3)] ring-1 ring-white/10 ring-inset dark:shadow-[0_6px_18px_-6px_rgba(56,189,248,0.6)]">
          <LogoMark size={18} />
        </span>
        <div className="min-w-0 max-lg:hidden">
          <div className="bg-linear-to-r from-white to-sky-200 bg-clip-text text-sm leading-5 font-semibold tracking-tight text-transparent">
            Duramem
          </div>
          <div className="text-2xs text-slate-500">切片即记忆</div>
        </div>
      </div>

      <nav aria-label="主导航" className="flex-1 overflow-y-auto px-3 py-4 max-lg:px-2.5">
        {NAV.map((group, index) => (
          <div key={group.group} className={index > 0 ? 'mt-5 max-lg:mt-3' : ''}>
            <div className="mb-1.5 px-2.5 text-2xs font-medium tracking-wide text-slate-600 max-lg:hidden">
              {group.group}
            </div>
            {index > 0 && <div className="mx-2 mb-3 h-px bg-white/[0.06] lg:hidden" />}
            <div className="space-y-0.5">
              {group.pages.map((item) => {
                const active = item.key === page
                const Icon = item.icon
                return (
                  <button
                    key={item.key}
                    type="button"
                    aria-label={item.label}
                    aria-current={active ? 'page' : undefined}
                    title={`${item.label} · ${item.hint}`}
                    onClick={() => onNavigate(item.key)}
                    className={`group relative flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-[13px] max-lg:justify-center ${
                      active
                        ? 'bg-sky-400/10 font-medium text-slate-50 ring-1 ring-sky-400/20 ring-inset'
                        : 'text-slate-400 hover:bg-white/[0.04] hover:text-slate-100'
                    }`}
                  >
                    {active && (
                      <span className="absolute top-1/2 left-0 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-linear-to-b from-cyan-300 to-sky-500" />
                    )}
                    <Icon
                      size={16}
                      className={`shrink-0 ${active ? 'text-sky-300' : 'text-slate-500 group-hover:text-slate-300'}`}
                    />
                    <span className="truncate max-lg:hidden">{item.label}</span>
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </nav>

      <StatusPanel health={health} loading={loading} fatal={fatal} />
    </aside>
  )
}

const STATE = {
  down: { label: '后端未连接', dot: 'bg-rose-400 shadow-[0_0_8px_rgba(251,113,133,0.8)]' },
  pending: { label: '连接中', dot: 'bg-slate-500' },
  offline: { label: '离线嵌入', dot: 'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,0.7)]' },
  live: { label: '真实嵌入', dot: 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]' },
}

function StatusPanel({
  health,
  loading,
  fatal,
}: {
  health: Health | null
  loading: boolean
  fatal: string | null
}) {
  const key = fatal ? 'down' : !health ? 'pending' : health.offline_embedding ? 'offline' : 'live'
  const { label, dot } = STATE[key]
  const provider = health ? (health.embedding_provider ?? health.embedding_model) : ''
  const summary = health
    ? `${label} · 重排${health.rerank_enabled ? '开' : '关'} · ${provider} · v${health.version}`
    : label

  return (
    <div className="shrink-0 border-t border-white/[0.06] p-3 max-lg:p-2.5">
      <div
        title={summary}
        className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-2.5 max-lg:flex max-lg:justify-center"
      >
        <div className="flex items-center gap-2">
          {loading && !health && !fatal ? (
            <span className="text-slate-400">
              <Spinner label="连接中" />
            </span>
          ) : (
            <span className={`size-2 shrink-0 rounded-full ${dot}`} />
          )}
          <span className="text-xs font-medium text-slate-200 max-lg:hidden">{label}</span>
          {health && (
            <span className="ml-auto text-2xs text-slate-600 tabular-nums max-lg:hidden">
              v{health.version}
            </span>
          )}
        </div>
        {health && (
          <div className="mt-2 space-y-1 text-2xs max-lg:hidden">
            <div className="flex items-center justify-between gap-2">
              <span className="text-slate-500">重排</span>
              <span className={health.rerank_enabled ? 'text-sky-300' : 'text-slate-400'}>
                {health.rerank_enabled ? '已开启' : '未开启'}
              </span>
            </div>
            <div className="truncate text-slate-500 mono" title={provider}>
              {provider}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
