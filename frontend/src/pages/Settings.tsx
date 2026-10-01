import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { SettingField, SettingsView } from '../types'
import { Badge, Button, Card, Empty, ErrorBox, KeyValue, NoticeBox, Stat } from '../components/ui'
import { IconCpu, IconInfo, IconPencil, IconRefresh, IconSliders, IconSparkles, IconUndo } from '../components/icons'
import SettingRow from './settings/SettingRow'

export default function Settings() {
  const [view, setView] = useState<SettingsView | null>(null)
  const [draft, setDraft] = useState<Record<string, number | string>>({})
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = useCallback(async () => {
    try {
      setView(await api.settings())
      setDraft({})
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const groups = useMemo(() => {
    if (!view) return []
    const map = new Map<string, SettingField[]>()
    for (const item of view.tunable) {
      const list = map.get(item.group) ?? []
      list.push(item)
      map.set(item.group, list)
    }
    return [...map.entries()]
  }, [view])

  const patch = async (payload: Record<string, unknown>) => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await api.patchSettings(payload)
      const rejected = Object.entries(result.rejected)
      if (rejected.length > 0) {
        setError(rejected.map(([key, reason]) => `${key}：${reason}`).join('；'))
      } else {
        setNotice(`已应用：${Object.keys(result.applied).join('、')}`)
      }
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const resetAll = async () => {
    setBusy(true)
    try {
      await api.resetSettings()
      setNotice('已清除全部运行时覆盖，回落到 .env 默认值')
      await reload()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  if (error && !view) return <ErrorBox>{error}</ErrorBox>
  if (!view) return <Empty icon={IconSliders}>正在读取设置…</Empty>

  const overriddenCount = view.tunable.filter((item) => item.overridden).length

  return (
    <div className="space-y-4">
      {error && <ErrorBox onClose={() => setError(null)}>{error}</ErrorBox>}
      {notice && (
        <NoticeBox tone="success" onClose={() => setNotice(null)}>
          {notice}
        </NoticeBox>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat icon={IconSliders} label="可改项" value={view.tunable.length} />
        <Stat icon={IconPencil} label="已覆盖" value={overriddenCount} hint="覆盖值存在 runtime_settings.json，优先于 .env" />
        <Stat icon={IconSparkles} label="嵌入模型" value={view.resolved.embedding_model} />
        <Stat
          icon={IconCpu}
          label="离线嵌入"
          value={view.resolved.offline_embedding ? '是' : '否'}
          hint="离线模式下用确定性哈希嵌入，仅供开发与测试"
        />
      </div>

      {view.resolved.offline_embedding && (
        <NoticeBox tone="warn">
          当前是<strong>离线嵌入</strong>（没有配置 API Key）。向量检索可用但语义泛化弱，距离阈值也需要按真实模型重新校准。
          到<strong>「模型」页</strong>填一个提供方即可。
        </NoticeBox>
      )}

      {overriddenCount > 0 && (
        <div className="flex items-center gap-3">
          <Button variant="ghost" icon={IconUndo} disabled={busy} onClick={() => void resetAll()}>
            全部恢复默认
          </Button>
          <span className="text-2xs text-slate-500">
            覆盖值写在 <span className="mono">{view.overrides_file}</span>
          </span>
        </div>
      )}

      <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
        {groups.map(([group, items]) => (
          <Card
            key={group}
            icon={IconSliders}
            title={group}
            subtitle={items.some((item) => item.overridden) ? '含已覆盖项' : undefined}
          >
            <div className="divide-y divide-white/[0.05]">
              {items.map((item) => (
                <div key={item.name} className="py-3 first:pt-0 last:pb-0">
                  <SettingRow
                    item={item}
                    draft={draft[item.name]}
                    disabled={busy}
                    onDraft={(value) => setDraft((prev) => ({ ...prev, [item.name]: value }))}
                    onToggle={(value) => void patch({ [item.name]: value })}
                    onCommit={() => {
                      const value = draft[item.name]
                      if (value === undefined || Number.isNaN(value)) return
                      void patch({ [item.name]: value })
                    }}
                    onReset={() => void patch({ [item.name]: item.value })}
                  />
                </div>
              ))}
            </div>
          </Card>
        ))}

        <Card icon={IconInfo} title="只读配置" subtitle="密钥、端口等不会从界面回显">
          <div className="dm-panel px-3.5 py-3">
            <KeyValue
              items={Object.entries(view.readonly).map(([key, value]) => ({
                label: key,
                value: String(value === '' ? '—' : value),
              }))}
            />
          </div>
          <p className="mt-3 text-2xs leading-relaxed text-slate-500">
            模型名、接口地址、API Key 在<strong className="font-medium text-slate-300">「模型」页</strong>
            配置——那边改完立即生效，密钥保存后只回显掩码。
          </p>
        </Card>

        <Card icon={IconRefresh} title="需要重启才生效的项">
          <div className="flex flex-wrap gap-1.5">
            {view.needs_restart.map((name) => (
              <Badge key={name}>{name}</Badge>
            ))}
          </div>
          <p className="mt-3 text-2xs leading-relaxed text-slate-500">
            这些字段要么得重建分词器、要么得重开监听，改完不重启就没有意义，所以明确列出来而不是静默忽略。
            模型相关字段<strong className="font-medium text-slate-300">不在这个列表里</strong>
            ：换嵌入模型是开发期最频繁的操作，走「模型」页改完立即生效。它们的风险不在重启，而在
            <strong className="font-medium text-slate-300">库里已有向量会作废</strong>——所以那边会明确列出需要重建的库。
          </p>
        </Card>
      </div>
    </div>
  )
}
