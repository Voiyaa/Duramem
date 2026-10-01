import type { ReactNode } from 'react'
import { IconInfo, type IconType } from './icons'

const formatValue = (value: ReactNode) =>
  typeof value === 'number' ? value.toLocaleString('zh-CN') : value

export function Stat({
  label,
  value,
  hint,
  icon: Icon,
}: {
  label: string
  value: ReactNode
  hint?: string
  icon?: IconType
}) {
  const shown = formatValue(value)
  return (
    <div
      title={hint}
      className="dm-stat"
    >
      <div className="dm-stat-label">
        {Icon && <Icon size={13} className="dm-stat-icon" />}
        <span className="truncate">{label}</span>
        {hint && <IconInfo size={12} className="dm-stat-hint-icon" />}
      </div>
      <div
        className="dm-stat-value"
        title={typeof shown === 'string' ? shown : undefined}
      >
        {shown}
      </div>
    </div>
  )
}

/** 行内的小指标：「切片 9」这种标签 + 数值。 */
export function Metric({ label, value, title }: { label: string; value: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="inline-flex h-6 items-center gap-1.5 rounded-md bg-slate-800/50 px-2 text-2xs ring-1 ring-white/5 ring-inset"
    >
      <span className="text-slate-500">{label}</span>
      <span className="font-medium text-slate-200 tabular-nums">{formatValue(value)}</span>
    </span>
  )
}

/** 颜色跟随 currentColor：放进主按钮里是白的，放在正文里是灰的。 */
export function Spinner({ label = '加载中' }: { label?: string }) {
  return (
    <span
      role="status"
      aria-label={label}
      className="inline-block size-3.5 shrink-0 animate-spin rounded-full border-2 border-current/25 border-t-current"
    />
  )
}

export function KeyValue({ items }: { items: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-2xs">
      {items.map((item) => (
        <div key={item.label} className="contents">
          <dt className="whitespace-nowrap text-slate-500">{item.label}</dt>
          <dd className="min-w-0 break-all text-slate-300">{item.value}</dd>
        </div>
      ))}
    </dl>
  )
}

/** 排名条：把名次可视化成一条横条，比纯数字更容易一眼比较。 */
export function RankBar({ rank, total }: { rank: number | null; total: number }) {
  if (rank === null) return <span className="text-2xs text-slate-600">—</span>
  const width = total > 0 ? Math.max(6, ((total - rank + 1) / total) * 100) : 100
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span className="w-6 shrink-0 text-right text-2xs font-medium text-slate-300 tabular-nums">
        #{rank}
      </span>
      <span className="h-1 flex-1 overflow-hidden rounded-full bg-slate-800">
        <span
          className="block h-full rounded-full bg-linear-to-r from-sky-500 to-cyan-300"
          style={{ width: `${width}%`, transition: 'width 0.3s ease' }}
        />
      </span>
    </div>
  )
}
