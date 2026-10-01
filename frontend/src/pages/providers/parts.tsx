import type { ProviderField, ProviderTestResult, ProviderView } from '../../types'
import { Badge, Card, Empty, Toggle } from '../../components/ui'
import { IconCheckCircle, IconDatabase, IconXCircle } from '../../components/icons'

/** 给输入框的 aria-label 加个分组后缀——三组里都有"API Key"和"模型名"。 */
const GROUP_HINT: Record<string, string> = { embedding: '嵌入', rerank: '重排', summary: '摘要' }

export function FieldRow({
  field,
  draft,
  disabled,
  onChange,
}: {
  field: ProviderField
  draft: string | undefined
  disabled: boolean
  onChange: (value: string) => void
}) {
  if (field.type === 'bool') {
    return (
      <div className="flex items-start gap-3">
        <div className="pt-0.5">
          <Toggle
            checked={draft === undefined ? Boolean(field.value) : draft === 'true'}
            onChange={(value) => onChange(String(value))}
            label={field.label}
            disabled={disabled}
          />
        </div>
        <div className="min-w-0 flex-1 pt-0.5 text-2xs leading-relaxed text-slate-500">{field.help}</div>
      </div>
    )
  }

  const isSecret = field.type === 'secret'
  const value = draft ?? (isSecret ? '' : field.value)

  return (
    <div className="grid grid-cols-[7rem_minmax(0,1fr)] items-center gap-x-3 gap-y-1">
      <span className="text-xs text-slate-300">{field.label}</span>
      <div className="flex min-w-0 items-center gap-2">
        <input
          type={isSecret ? 'password' : field.type === 'int' ? 'number' : 'text'}
          value={value}
          disabled={disabled}
          // 密钥字段的 placeholder 是空的（有值时才提示"留空保持不变"），
          // 没有 aria-label 的话这个输入框在无障碍树里就是个无名控件
          aria-label={`${field.label}（${GROUP_HINT[field.group] ?? field.group}）`}
          placeholder={isSecret && field.has_value ? '已配置（留空保持不变）' : field.placeholder}
          onChange={(event) => onChange(event.target.value)}
          className="dm-field mono"
        />
        {isSecret && field.has_value && <Badge tone="sky">已配置</Badge>}
        {field.overridden && !isSecret && <Badge>已覆盖</Badge>}
      </div>
      {field.help && <div className="col-start-2 text-2xs leading-relaxed text-slate-500">{field.help}</div>}
    </div>
  )
}

export function TestResult({ result }: { result: ProviderTestResult }) {
  const lines: string[] = []
  if (result.ms !== undefined) lines.push(`${result.ms} ms`)
  if (result.detected_dim !== undefined) lines.push(`${result.detected_dim} 维`)
  const Icon = result.ok ? IconCheckCircle : IconXCircle

  return (
    <div
      role="status"
      className={`flex animate-fade-up items-start gap-2.5 rounded-xl border px-3.5 py-2.5 text-2xs ${
        result.ok
          ? 'border-emerald-400/25 bg-emerald-400/[0.07] text-emerald-100'
          : 'border-rose-400/30 bg-rose-500/[0.08] text-rose-100'
      }`}
    >
      <Icon size={15} className={`mt-px shrink-0 ${result.ok ? 'text-emerald-300' : 'text-rose-300'}`} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className="font-medium">
            {result.ok ? (result.offline ? '可用（离线降级）' : '连接正常') : '测试失败'}
          </span>
          {result.provider && <span className="opacity-80 mono">{result.provider}</span>}
          {lines.length > 0 && <span className="opacity-70 tabular-nums">{lines.join(' · ')}</span>}
        </div>
        {result.note && <div className="mt-1 whitespace-pre-wrap">{result.note}</div>}
        {result.error && <div className="mt-1 break-all whitespace-pre-wrap">{result.error}</div>}
      </div>
    </div>
  )
}

export function VectorSourcesCard({ databases }: { databases: ProviderView['databases'] }) {
  return (
    <Card icon={IconDatabase} title="库内向量的来源" subtitle="换模型后这些库需要重建，重建前检索会被拒绝">
      {databases.length === 0 ? (
        <Empty compact>还没有库</Empty>
      ) : (
        <div className="dm-panel divide-y divide-white/[0.05]">
          {databases.map((item) => {
            const source = item.error
              ? item.error
              : !item.source_known
                ? '未记录（改版前建的库，改模型时会自动记为当时的提供方）'
                : item.vector_source || '还没写过向量'
            return (
              <div key={item.name} className="flex items-center gap-3 px-3.5 py-2.5 text-2xs">
                <span className="w-28 shrink-0 truncate font-medium text-slate-200">{item.name}</span>
                <span className="min-w-0 flex-1 truncate text-slate-500 mono" title={source}>
                  {source}
                </span>
                {item.needs_rebuild ? (
                  <Badge tone="rose">需重建</Badge>
                ) : item.error ? (
                  <Badge tone="rose">打不开</Badge>
                ) : !item.source_known ? (
                  <Badge tone="amber">来源未记录</Badge>
                ) : item.chunks === 0 ? (
                  <Badge>空库</Badge>
                ) : (
                  <Badge tone="emerald">一致</Badge>
                )}
              </div>
            )
          })}
        </div>
      )}
      <p className="mt-3 text-2xs leading-relaxed text-slate-500">
        来源指纹记在库文件的 <code>db_meta.vector_source</code> 里，记录的是
        <strong className="font-medium text-slate-300">实际跑的那个实现</strong>
        ，不是配置里写的模型名——没填 Key 时它们是两回事。
      </p>
    </Card>
  )
}
