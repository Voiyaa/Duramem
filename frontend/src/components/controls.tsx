import type { ReactNode } from 'react'
import type { IconType } from './icons'

/* 交互控件。外观在 styles/buttons.css 与 styles/fields.css 的组件层里，
   所以调用方传进来的 Tailwind 工具类（宽度、内边距）总能覆盖默认值。 */

export function Button({
  children,
  onClick,
  variant = 'default',
  size = 'md',
  icon: Icon,
  iconOnly = false,
  disabled,
  title,
  type = 'button',
  className = '',
  'aria-label': ariaLabel,
}: {
  children?: ReactNode
  onClick?: () => void
  variant?: 'default' | 'primary' | 'danger' | 'ghost'
  size?: 'sm' | 'md'
  icon?: IconType
  /** 只显示图标；此时 title 兼作无障碍名称。 */
  iconOnly?: boolean
  disabled?: boolean
  title?: string
  type?: 'button' | 'submit'
  className?: string
  'aria-label'?: string
}) {
  return (
    <button
      type={type}
      title={title}
      aria-label={ariaLabel ?? (iconOnly ? title : undefined)}
      disabled={disabled}
      onClick={onClick}
      className={`dm-btn dm-btn-${variant} ${size === 'sm' ? 'dm-btn-sm' : ''} ${iconOnly ? 'dm-btn-icon' : ''} ${className}`}
    >
      {Icon && <Icon size={size === 'sm' ? 13 : 14} className="shrink-0" />}
      {!iconOnly && children}
    </button>
  )
}

export function Input({
  value,
  onChange,
  placeholder,
  onEnter,
  className = '',
  icon: Icon,
  id,
  ariaLabel,
  disabled,
}: {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  onEnter?: () => void
  className?: string
  icon?: IconType
  id?: string
  ariaLabel?: string
  disabled?: boolean
}) {
  const field = (
    <input
      id={id}
      value={value}
      placeholder={placeholder}
      aria-label={ariaLabel}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        // 中文输入法选词时的回车不是"提交"
        if (event.key === 'Enter' && !event.nativeEvent.isComposing && onEnter) onEnter()
      }}
      className={`dm-field ${Icon ? 'pl-8' : ''} ${className}`}
    />
  )
  if (!Icon) return field
  return (
    <div className="relative w-full">
      <Icon
        size={14}
        className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-slate-500"
      />
      {field}
    </div>
  )
}

export function TextArea({
  value,
  onChange,
  rows = 4,
  placeholder,
  className = '',
  mono = false,
  disabled,
  id,
}: {
  value: string
  onChange: (value: string) => void
  rows?: number
  placeholder?: string
  className?: string
  mono?: boolean
  disabled?: boolean
  id?: string
}) {
  return (
    <textarea
      id={id}
      value={value}
      rows={rows}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      className={`dm-field ${mono ? 'mono' : ''} ${className}`}
    />
  )
}

export function Select<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
  className = '',
}: {
  value: T
  onChange: (value: T) => void
  options: { value: T; label: string }[]
  ariaLabel?: string
  className?: string
}) {
  return (
    <select
      value={value}
      aria-label={ariaLabel}
      onChange={(event) => onChange(event.target.value as T)}
      className={`dm-field ${className}`}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  )
}

export function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean
  onChange: (value: boolean) => void
  label?: ReactNode
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="inline-flex items-center gap-2 text-xs text-slate-300 hover:text-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
    >
      <span
        className={`relative inline-flex h-[18px] w-8 shrink-0 items-center rounded-full ring-1 ring-inset transition-colors duration-200 ${
          checked
            ? 'bg-sky-500 shadow-[0_0_12px_-2px_rgba(14,165,233,0.6)] ring-sky-300/50'
            : 'bg-slate-700/70 ring-white/10'
        }`}
      >
        <span
          className={`dm-toggle-knob absolute left-0.5 size-3.5 rounded-full shadow-sm transition-transform duration-200 ${
            checked ? 'translate-x-3.5' : ''
          }`}
        />
      </span>
      {label}
    </button>
  )
}

/** 互斥的少量选项（≤6 个）用分段按钮，比下拉框少一次点击、也一眼看得见全部选项。 */
export function Segmented<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
}: {
  value: T
  onChange: (value: T) => void
  options: { value: T; label: ReactNode; title?: string }[]
  ariaLabel?: string
}) {
  return (
    <div role="group" aria-label={ariaLabel} className="dm-seg">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          title={option.title}
          aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}
          className="dm-seg-item"
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
