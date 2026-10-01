import { useState } from 'react'
import type { ActiveDbInfo } from '../types'
import type { PageMeta } from '../routes'
import { Button } from './controls'
import { IconDatabase, IconTarget, IconSun, IconMoon } from './icons'
import { getTheme, toggleTheme, type Theme } from '../theme'

/**
 * 顶栏右侧把两件常被混淆的事并排摆出来：
 * 「浏览」只影响界面看哪个库；「记忆库」才是模型检索与写入的那个库。
 */
export default function TopBar({
  meta,
  databases,
  db,
  onSelectDb,
  active,
  loading,
  onSetActive,
}: {
  meta: PageMeta
  databases: string[]
  db: string
  onSelectDb: (name: string) => void
  active: ActiveDbInfo | null
  loading: boolean
  onSetActive: (name: string) => void
}) {
  const Icon = meta.icon
  const activeName = active?.db && active.exists ? String(active.db) : null
  const [theme, setThemeState] = useState<Theme>(getTheme)

  const handleToggleTheme = () => {
    const next = toggleTheme()
    setThemeState(next)
  }

  return (
    <header className="relative z-10 flex h-14 shrink-0 items-center gap-4 border-b border-white/[0.06] bg-ink-950/60 px-5 backdrop-blur-xl">
      <div className="flex min-w-0 items-center gap-3">
        <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-sky-400/10 text-sky-300 ring-1 ring-sky-400/20 ring-inset">
          <Icon size={16} />
        </span>
        <div className="min-w-0">
          <h1 className="truncate text-sm leading-5 font-semibold text-slate-50">{meta.label}</h1>
          <p className="truncate text-2xs text-slate-500">{meta.hint}</p>
        </div>
      </div>

      <div className="ml-auto flex min-w-0 items-center gap-2">
        <button
          onClick={handleToggleTheme}
          className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-slate-700/60 text-slate-400 transition hover:border-slate-600 hover:text-slate-300"
          title={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
          aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
        >
          {theme === 'dark' ? <IconSun size={15} /> : <IconMoon size={15} />}
        </button>

        {databases.length > 0 ? (
          <label className="flex items-center gap-2 text-2xs text-slate-500">
            <span className="max-md:hidden">浏览</span>
            <span className="relative">
              <IconDatabase
                size={13}
                className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-slate-500"
              />
              <select
                value={db}
                aria-label="浏览哪个库"
                onChange={(event) => onSelectDb(event.target.value)}
                className="dm-field max-w-[12rem] pl-7"
              >
                {databases.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </span>
          </label>
        ) : (
          <span className="text-xs text-slate-500">还没有库</span>
        )}

        <span className="mx-1 h-5 w-px bg-white/[0.08]" aria-hidden="true" />

        {loading && !active ? null : activeName ? (
          <span
            title={`模型读写的是这个库。选定于 ${active?.updated_at ?? '未知时间'}（${active?.updated_by ?? '?'}）。在「库管理」页可以改。`}
            className="inline-flex h-8 items-center gap-2 rounded-full border border-violet-400/25 bg-violet-400/[0.08] pr-3 pl-2.5 text-2xs"
          >
            <span className="size-1.5 rounded-full bg-violet-400 shadow-[0_0_8px_rgba(167,139,250,0.9)]" />
            <span className="text-violet-200/70">记忆库</span>
            <span className="max-w-[10rem] truncate font-medium text-violet-50">{activeName}</span>
          </span>
        ) : (
          <span
            title={active?.error ?? undefined}
            className="inline-flex h-8 items-center gap-2 rounded-full border border-amber-400/25 bg-amber-400/[0.08] px-3 text-2xs text-amber-200"
          >
            <span className="size-1.5 rounded-full bg-amber-400" />
            {active?.db ? `记忆库「${active.db}」已不存在，请重新选定` : '未选定记忆库'}
            {active?.fallback ? `（暂用 ${active.fallback}）` : ''}
          </span>
        )}

        {db && db !== activeName && (
          <Button
            size="sm"
            variant="ghost"
            icon={IconTarget}
            title={`把「${db}」设为模型读写的记忆库`}
            onClick={() => onSetActive(db)}
          >
            设为记忆库
          </Button>
        )}
      </div>
    </header>
  )
}
