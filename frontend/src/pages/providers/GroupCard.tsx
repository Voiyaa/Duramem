import type { ReactNode } from 'react'
import type { ProviderField, ProviderPreset, ProviderTestResult, ProviderView } from '../../types'
import { Button, Card, KeyValue, Spinner } from '../../components/ui'
import { IconCpu, IconNote, IconPlug, IconSparkles, IconTrending, type IconType } from '../../components/icons'
import { FieldRow, TestResult } from './parts'
import type { GroupId } from './useProviders'

const GROUP_ICON: Record<string, IconType> = {
  embedding: IconSparkles,
  rerank: IconTrending,
  summary: IconNote,
}

const masked = (hasKey: boolean, mask: ReactNode) => (hasKey ? <span className="mono">{mask}</span> : '未配置')

function keyInfoFor(id: string, view: ProviderView) {
  if (id === 'embedding') {
    const current = view.effective_embedding
    return [
      { label: '生效提供方', value: current.provider },
      { label: 'API Key', value: masked(current.has_key, current.key_masked) },
    ]
  }
  if (id === 'rerank') {
    return [
      { label: '生效重排', value: view.rerank.effective },
      { label: 'API Key', value: masked(view.rerank.has_key, view.rerank.key_masked) },
    ]
  }
  if (id === 'summary') {
    return [
      { label: '生效摘要', value: view.summary.effective },
      { label: 'API Key', value: masked(view.summary.has_key, view.summary.key_masked) },
    ]
  }
  return null
}

export default function GroupCard({
  group,
  view,
  fields,
  draft,
  busy,
  testing,
  result,
  onTest,
  onPreset,
  onChange,
}: {
  group: ProviderView['groups'][number]
  view: ProviderView
  fields: ProviderField[]
  draft: Record<string, string>
  busy: boolean
  testing: GroupId | null
  result?: ProviderTestResult
  onTest: () => void
  onPreset: (preset: ProviderPreset) => void
  onChange: (name: string, value: string) => void
}) {
  const presets = view.presets[group.id] ?? []
  const keyInfo = keyInfoFor(group.id, view)
  const running = testing === group.id

  return (
    <Card
      icon={GROUP_ICON[group.id] ?? IconCpu}
      title={group.label}
      subtitle={group.help}
      right={
        <Button
          size="sm"
          icon={running ? undefined : IconPlug}
          disabled={testing !== null || busy}
          onClick={onTest}
          title="用当前输入框里的值试一次调用（不影响已保存的配置）"
        >
          {running && <Spinner label="测试中" />}
          {running ? '测试中…' : '测试连接'}
        </Button>
      }
    >
      <div className="space-y-4">
        {presets.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="mr-1 text-2xs text-slate-500">预设</span>
            {presets.map((preset) => (
              <button
                key={preset.id}
                type="button"
                className="dm-chip"
                disabled={busy}
                title={preset.note}
                onClick={() => onPreset(preset)}
              >
                <span className="text-slate-500">{preset.vendor}</span>
                {preset.label}
              </button>
            ))}
          </div>
        )}

        <div className="space-y-3">
          {fields.map((field) => (
            <FieldRow
              key={field.name}
              field={field}
              draft={draft[field.name]}
              disabled={busy}
              onChange={(value) => onChange(field.name, value)}
            />
          ))}
        </div>

        {keyInfo && (
          <div className="dm-panel px-3.5 py-2.5">
            <KeyValue items={keyInfo} />
          </div>
        )}

        {result && <TestResult result={result} />}
      </div>
    </Card>
  )
}
