import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react'
import { createPortal, flushSync } from 'react-dom'
import { IconMore, type IconType } from './icons'

export type MenuEntry =
  | 'separator'
  | {
      label: string
      description?: string
      icon?: IconType
      danger?: boolean
      disabled?: boolean
      onSelect: () => void
    }

/**
 * 行尾的「更多」菜单。渲染进 body 并按触发按钮定位：
 * 卡片为了圆角裁切开了 overflow-hidden，放在卡片里会被切掉。
 */
export function Menu({ items, label = '更多操作' }: { items: MenuEntry[]; label?: string }) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  const close = useCallback((restoreFocus: boolean) => {
    setOpen(false)
    setPos(null)
    if (restoreFocus) triggerRef.current?.focus()
  }, [])

  useLayoutEffect(() => {
    if (!open) return
    const trigger = triggerRef.current?.getBoundingClientRect()
    const menu = menuRef.current
    if (!trigger || !menu) return
    const width = menu.offsetWidth
    const height = menu.offsetHeight
    const left = Math.max(8, Math.min(trigger.right - width, window.innerWidth - width - 8))
    const below = trigger.bottom + 6
    const top =
      below + height > window.innerHeight - 8 ? Math.max(8, trigger.top - height - 6) : below
    setPos({ top, left })
  }, [open])

  useEffect(() => {
    if (!pos) return
    menuRef.current
      ?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')
      ?.focus({ preventScroll: true })
  }, [pos])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      close(false)
    }
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') close(true)
    }
    const onMove = () => close(false)
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onMove, true)
    window.addEventListener('resize', onMove)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', onMove, true)
      window.removeEventListener('resize', onMove)
    }
  }, [open, close])

  const onMenuKey = (event: KeyboardEvent<HTMLDivElement>) => {
    const nodes = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? [],
    )
    if (nodes.length === 0) return
    const index = nodes.indexOf(document.activeElement as HTMLButtonElement)
    let next = -1
    if (event.key === 'ArrowDown') next = (index + 1) % nodes.length
    else if (event.key === 'ArrowUp') next = index <= 0 ? nodes.length - 1 : index - 1
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = nodes.length - 1
    else if (event.key === 'Tab') close(false)
    if (next >= 0) {
      event.preventDefault()
      nodes[next].focus()
    }
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => (open ? close(false) : setOpen(true))}
        className={`dm-btn dm-btn-ghost dm-btn-sm dm-btn-icon ${open ? 'bg-white/10 text-slate-100' : ''}`}
      >
        <IconMore size={15} />
      </button>
      {open &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label={label}
            onKeyDown={onMenuKey}
            className="dm-menu"
            style={pos ?? { top: 0, left: 0, visibility: 'hidden' }}
          >
            {items.map((item, index) =>
              item === 'separator' ? (
                <div key={`sep-${index}`} role="separator" className="dm-menu-sep" />
              ) : (
                <MenuItem
                  key={item.label}
                  item={item}
                  onPick={() => {
                    // 先把菜单真正关掉再执行：onSelect 里常有 prompt/confirm，
                    // 它们会阻塞渲染，不 flush 的话菜单会悬在对话框后面
                    flushSync(() => close(true))
                    item.onSelect()
                  }}
                />
              ),
            )}
          </div>,
          document.body,
        )}
    </>
  )
}

function MenuItem({ item, onPick }: { item: Exclude<MenuEntry, 'separator'>; onPick: () => void }) {
  const Icon = item.icon
  return (
    <button
      type="button"
      role="menuitem"
      disabled={item.disabled}
      data-danger={item.danger ? '' : undefined}
      onClick={onPick}
      className="dm-menu-item"
    >
      {Icon ? (
        <Icon size={15} className="mt-px shrink-0 opacity-80" />
      ) : (
        <span className="w-[15px] shrink-0" />
      )}
      <span className="min-w-0">
        <span className="block font-medium">{item.label}</span>
        {item.description && (
          <span className="mt-0.5 block text-2xs leading-4 opacity-60">{item.description}</span>
        )}
      </span>
    </button>
  )
}
