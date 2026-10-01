import type { ReactNode } from 'react'
import type { Arm } from '../types'
import {
  IconAlert,
  IconCheckCircle,
  IconInbox,
  IconInfo,
  IconX,
  IconXCircle,
  type IconType,
} from './icons'

export * from './controls'
export * from './data'
export { Menu, type MenuEntry } from './Menu'

/* ====================================================================== 容器 */

export function Card({
  title,
  subtitle,
  right,
  icon: Icon,
  children,
  className = '',
  bodyClassName = '',
}: {
  title?: ReactNode
  subtitle?: ReactNode
  right?: ReactNode
  icon?: IconType
  children: ReactNode
  className?: string
  bodyClassName?: string
}) {
  return (
    <section className={`dm-card ${className}`}>
      {(title || right) && (
        <header className="dm-card-header">
          <div className="flex min-w-0 items-start gap-2.5">
            {Icon && (
              <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-sky-400/10 text-sky-300 ring-1 ring-sky-400/20 ring-inset">
                <Icon size={15} />
              </span>
            )}
            <div className="min-w-0">
              {title && (
                <h2 className="truncate text-[13px] leading-7 font-semibold text-slate-50">{title}</h2>
              )}
              {subtitle && (
                <p className="-mt-0.5 text-2xs leading-relaxed text-slate-500">{subtitle}</p>
              )}
            </div>
          </div>
          {right && <div className="flex shrink-0 items-center gap-1.5">{right}</div>}
        </header>
      )}
      <div className={`dm-card-body ${bodyClassName}`}>{children}</div>
    </section>
  )
}

export function FieldLabel({
  children,
  hint,
  htmlFor,
  right,
}: {
  children: ReactNode
  hint?: ReactNode
  htmlFor?: string
  right?: ReactNode
}) {
  return (
    <div className="flex min-h-5 items-center justify-between gap-2">
      <label htmlFor={htmlFor} className="text-2xs font-medium text-slate-300">
        {children}
        {hint && <span className="ml-1.5 font-normal text-slate-500">{hint}</span>}
      </label>
      {right}
    </div>
  )
}

export function Empty({
  children,
  icon: Icon = IconInbox,
  compact = false,
}: {
  children: ReactNode
  icon?: IconType
  compact?: boolean
}) {
  if (compact) {
    return <div className="px-3 py-4 text-center text-xs leading-relaxed text-slate-500">{children}</div>
  }
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-10 text-center">
      <span className="grid size-10 place-items-center rounded-xl bg-slate-800/50 text-slate-500 ring-1 ring-white/5 ring-inset">
        <Icon size={18} />
      </span>
      <div className="max-w-md text-xs leading-relaxed text-slate-500">{children}</div>
    </div>
  )
}

/* ====================================================================== 提示 */

type Tone = 'info' | 'warn' | 'success' | 'error'

const ALERT: Record<Tone, { box: string; icon: IconType; iconClass: string }> = {
  info: { box: 'border-sky-400/20 bg-sky-400/[0.06] text-sky-50', icon: IconInfo, iconClass: 'text-sky-300' },
  warn: { box: 'border-amber-400/25 bg-amber-400/[0.07] text-amber-50', icon: IconAlert, iconClass: 'text-amber-300' },
  success: { box: 'border-emerald-400/25 bg-emerald-400/[0.07] text-emerald-50', icon: IconCheckCircle, iconClass: 'text-emerald-300' },
  error: { box: 'border-rose-400/30 bg-rose-500/[0.08] text-rose-50', icon: IconXCircle, iconClass: 'text-rose-300' },
}

export function Alert({
  tone = 'info',
  children,
  onClose,
}: {
  tone?: Tone
  children: ReactNode
  onClose?: () => void
}) {
  const style = ALERT[tone]
  const Icon = style.icon
  return (
    <div
      role={tone === 'error' ? 'alert' : 'status'}
      className={`flex animate-fade-up items-start gap-2.5 rounded-xl border px-3.5 py-2.5 text-xs leading-relaxed ${style.box}`}
    >
      <Icon size={15} className={`mt-0.5 shrink-0 ${style.iconClass}`} />
      <div className="min-w-0 flex-1">{children}</div>
      {onClose && (
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭提示"
          className="-m-1 shrink-0 rounded-md p-1 opacity-60 hover:bg-white/10 hover:opacity-100"
        >
          <IconX size={14} />
        </button>
      )}
    </div>
  )
}

export function ErrorBox({ children, onClose }: { children: ReactNode; onClose?: () => void }) {
  return (
    <Alert tone="error" onClose={onClose}>
      {children}
    </Alert>
  )
}

/** 默认是中性的"信息"色；只有真需要用户留神的（告警、降级）才传 tone="warn"。 */
export function NoticeBox({
  children,
  tone = 'info',
  onClose,
}: {
  children: ReactNode
  tone?: Tone
  onClose?: () => void
}) {
  return (
    <Alert tone={tone} onClose={onClose}>
      {children}
    </Alert>
  )
}

/* ====================================================================== 标记 */

type BadgeTone = 'slate' | 'sky' | 'emerald' | 'amber' | 'rose' | 'violet'

const BADGE: Record<BadgeTone, [string, string]> = {
  slate: ['bg-slate-400/10 text-slate-300 ring-slate-400/20', 'bg-slate-400'],
  sky: ['bg-sky-400/10 text-sky-300 ring-sky-400/25', 'bg-sky-400'],
  emerald: ['bg-emerald-400/10 text-emerald-300 ring-emerald-400/25', 'bg-emerald-400'],
  amber: ['bg-amber-400/10 text-amber-300 ring-amber-400/25', 'bg-amber-400'],
  rose: ['bg-rose-400/10 text-rose-300 ring-rose-400/25', 'bg-rose-400'],
  violet: ['bg-violet-400/10 text-violet-300 ring-violet-400/25', 'bg-violet-400'],
}

export function Badge({
  children,
  tone = 'slate',
  dot = false,
  title,
}: {
  children: ReactNode
  tone?: BadgeTone
  dot?: boolean
  title?: string
}) {
  const [box, dotColor] = BADGE[tone]
  return (
    <span
      title={title}
      className={`inline-flex h-5 shrink-0 items-center gap-1 rounded-full px-2 text-2xs font-medium whitespace-nowrap ring-1 ring-inset ${box}`}
    >
      {dot && <span className={`size-1.5 rounded-full ${dotColor}`} />}
      {children}
    </span>
  )
}

/** 两路命中情况的可视标记。调试面板靠它一眼看出每条候选是哪一路捞回来的。 */
export function ArmBadge({ arm }: { arm: Arm }) {
  if (arm === 'both') return <Badge tone="emerald" dot>两路都命中</Badge>
  if (arm === 'vector_only') return <Badge tone="sky" dot>仅向量</Badge>
  if (arm === 'lexical_only') return <Badge tone="amber" dot>仅词法</Badge>
  return <Badge dot>未命中</Badge>
}

export function armAccent(arm: Arm): string {
  if (arm === 'both') return 'border-l-emerald-400/70'
  if (arm === 'vector_only') return 'border-l-sky-400/70'
  if (arm === 'lexical_only') return 'border-l-amber-400/70'
  return 'border-l-slate-700'
}
