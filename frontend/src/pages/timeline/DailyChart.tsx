import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

export type DailyPoint = { date: string; 新增: number; 命中: number }

const SERIES: Record<string, string> = { 新增: 'bg-sky-400', 命中: 'bg-violet-400' }
const AXIS_TICK = { fill: '#6f819c', fontSize: 11 }

export default function DailyChart({ data }: { data: DailyPoint[] }) {
  return (
    <div className="h-60">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          margin={{ top: 8, right: 8, bottom: 0, left: -18 }}
          // 只有一个日期分组时，recharts 会让每根柱子各占一半图宽，看起来像坏了。
          // 限制最大柱宽并留出分组间距，一根和十根数据的观感都正常。
          barCategoryGap="35%"
          barGap={4}
        >
          <defs>
            <linearGradient id="dm-bar-new" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#38bdf8" />
              <stop offset="100%" stopColor="#0369a1" stopOpacity={0.55} />
            </linearGradient>
            <linearGradient id="dm-bar-hit" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#a78bfa" />
              <stop offset="100%" stopColor="#6d28d9" stopOpacity={0.55} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 4" stroke="rgba(148,163,184,0.1)" vertical={false} />
          <XAxis
            dataKey="date"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: 'rgba(148,163,184,0.15)' }}
          />
          <YAxis tick={AXIS_TICK} tickLine={false} axisLine={false} allowDecimals={false} />
          <Tooltip cursor={{ fill: 'rgba(148,163,184,0.06)' }} content={<ChartTooltip />} />
          <Bar dataKey="新增" fill="url(#dm-bar-new)" radius={[4, 4, 0, 0]} maxBarSize={40} />
          <Bar dataKey="命中" fill="url(#dm-bar-hit)" radius={[4, 4, 0, 0]} maxBarSize={40} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

/** 柱子用的是渐变填充，recharts 默认提示框会拿 url(#…) 当文字颜色——所以自己画。 */
function ChartTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean
  payload?: { name?: string | number; value?: number | string }[]
  label?: string | number
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="min-w-32 rounded-lg border border-white/10 bg-slate-900/95 px-3 py-2 text-2xs shadow-xl backdrop-blur">
      <div className="mb-1.5 font-medium text-slate-200">{label}</div>
      {payload.map((entry) => (
        <div key={String(entry.name)} className="flex items-center gap-2 text-slate-400">
          <span className={`size-2 rounded-full ${SERIES[String(entry.name)] ?? 'bg-slate-400'}`} />
          {entry.name}
          <span className="ml-auto pl-4 font-medium text-slate-100 tabular-nums">{entry.value}</span>
        </div>
      ))}
    </div>
  )
}

export function ChartLegend() {
  return (
    <div className="flex items-center gap-3 text-2xs text-slate-400">
      {Object.entries(SERIES).map(([name, color]) => (
        <span key={name} className="inline-flex items-center gap-1.5">
          <span className={`size-2 rounded-full ${color}`} />
          {name}
        </span>
      ))}
    </div>
  )
}
