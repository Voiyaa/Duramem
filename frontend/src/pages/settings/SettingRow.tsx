import { useId } from 'react'
import type { SettingField } from '../../types'
import { Badge, Button, TextArea, Toggle } from '../../components/ui'

type Props = {
  item: SettingField
  draft: number | string | undefined
  disabled: boolean
  onDraft: (value: number | string) => void
  onToggle: (value: boolean) => void
  onCommit: () => void
  onReset: () => void
}

/** 数字输入框放在行尾：右侧按钮有无、宽窄不一时，各行的输入框仍然对齐。 */
export default function SettingRow({ item, draft, disabled, onDraft, onToggle, onCommit, onReset }: Props) {
  const id = useId()

  if (item.type === 'bool') {
    return (
      <div className="flex items-start gap-3">
        <div className="pt-0.5">
          <Toggle checked={Boolean(item.value)} onChange={onToggle} label={item.label} disabled={disabled} />
        </div>
        <div className="flex min-w-0 flex-1 items-start gap-2 pt-0.5 text-2xs leading-relaxed text-slate-500">
          <span className="min-w-0 flex-1">{item.help}</span>
          {item.overridden && <Badge tone="sky">已覆盖</Badge>}
        </div>
      </div>
    )
  }

  if (item.type === 'string') {
    const current = typeof draft === 'string' ? draft : String(item.value ?? '')
    const dirty = draft !== undefined && draft !== String(item.value ?? '')
    return (
      <div className="space-y-1.5">
        <div className="flex min-h-7 items-center gap-2">
          <label htmlFor={id} className="min-w-0 flex-1 text-xs text-slate-200">
            {item.label}
          </label>
          <Actions dirty={dirty} overridden={item.overridden} disabled={disabled} onCommit={onCommit} onReset={onReset} />
        </div>
        <TextArea id={id} rows={4} value={current} disabled={disabled} placeholder="留空 = 不注入" onChange={onDraft} />
        <p className="text-2xs leading-relaxed text-slate-500">{item.help}</p>
      </div>
    )
  }

  const current = draft ?? Number(item.value)
  const dirty = draft !== undefined && draft !== Number(item.value)
  return (
    <div>
      <div className="flex min-h-7 items-center gap-2">
        <label htmlFor={id} className="min-w-0 flex-1 text-xs text-slate-200">
          {item.label}
        </label>
        <Actions dirty={dirty} overridden={item.overridden} disabled={disabled} onCommit={onCommit} onReset={onReset} />
        <input
          id={id}
          type="number"
          step={item.type === 'float' ? 0.05 : 1}
          value={Number.isNaN(current) ? '' : current}
          disabled={disabled}
          onChange={(event) => onDraft(Number(event.target.value))}
          onKeyDown={(event) => {
            if (event.key === 'Enter') onCommit()
          }}
          className={`dm-field w-24 shrink-0 text-right tabular-nums ${dirty ? 'border-sky-400/50' : ''}`}
        />
      </div>
      <p className="mt-1 text-2xs leading-relaxed text-slate-500">{item.help}</p>
    </div>
  )
}

function Actions({
  dirty,
  overridden,
  disabled,
  onCommit,
  onReset,
}: {
  dirty: boolean
  overridden: boolean
  disabled: boolean
  onCommit: () => void
  onReset: () => void
}) {
  if (dirty) {
    return (
      <Button size="sm" variant="primary" disabled={disabled} onClick={onCommit}>
        应用
      </Button>
    )
  }
  if (!overridden) return null
  return (
    <>
      <Badge tone="sky">已覆盖</Badge>
      <Button size="sm" variant="ghost" disabled={disabled} onClick={onReset} title="回落到 .env 默认值">
        恢复
      </Button>
    </>
  )
}
