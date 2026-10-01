import { useCallback, useEffect, useMemo, useState } from 'react'
import { providerApi } from '../../api'
import type { ProviderField, ProviderPreset, ProviderTestResult, ProviderView } from '../../types'
import { collectDirty, describeSave, testValues } from './logic'

export type GroupId = 'embedding' | 'rerank' | 'summary'

const message = (err: unknown) => (err instanceof Error ? err.message : String(err))

/** 模型配置页的全部状态与动作；页面组件只管摆放。 */
export function useProviders(onChanged?: () => void) {
  const [view, setView] = useState<ProviderView | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [testing, setTesting] = useState<GroupId | null>(null)
  const [results, setResults] = useState<Partial<Record<GroupId, ProviderTestResult>>>({})
  const [rebuilding, setRebuilding] = useState<string | null>(null)
  const [staleAfterSave, setStaleAfterSave] = useState<string[]>([])

  const reload = useCallback(async () => {
    try {
      setView(await providerApi.view())
      setError(null)
    } catch (err) {
      setError(message(err))
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const byGroup = useMemo(() => {
    const map: Record<GroupId, ProviderField[]> = { embedding: [], rerank: [], summary: [] }
    for (const field of view?.fields ?? []) map[field.group].push(field)
    return map
  }, [view])

  const dirtyCount = useMemo(() => Object.keys(collectDirty(draft, view?.fields ?? [])).length, [draft, view])

  const set = (name: string, value: string) => {
    setDraft((prev) => ({ ...prev, [name]: value }))
    // 输入框一变，上一次的测试结果就不再描述当前输入了。留着它比没有更糟：
    // 用户会以为"刚才测过了、没问题"。
    const group = view?.fields.find((item) => item.name === name)?.group
    if (group) {
      setResults((prev) => {
        if (!(group in prev)) return prev
        const next = { ...prev }
        delete next[group]
        return next
      })
    }
  }

  const applyPreset = (preset: ProviderPreset) => {
    setDraft((prev) => {
      const next = { ...prev }
      for (const [key, value] of Object.entries(preset.values)) next[key] = String(value)
      return next
    })
    setResults({})
    setNotice(
      `已填入「${preset.vendor} ${preset.label}」${preset.needs_key ? '，还需要填 API Key' : ''}。` +
        '注意这些只是草稿，点「测试连接」验证后再保存。',
    )
  }

  const runTest = async (group: GroupId) => {
    setTesting(group)
    setError(null)
    setNotice(null)
    try {
      const result = await providerApi.test(group, testValues(draft, byGroup[group]))
      setResults((prev) => ({ ...prev, [group]: result }))
    } catch (err) {
      setResults((prev) => ({ ...prev, [group]: { ok: false, group, error: message(err) } }))
    } finally {
      setTesting(null)
    }
  }

  const save = async () => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const outcome = describeSave(await providerApi.patch(collectDirty(draft, view?.fields ?? [])))
      setError(outcome.error)
      setNotice(outcome.notice)
      if (outcome.stale) setStaleAfterSave(outcome.stale)
      setDraft({})
      // 保存后输入框被清空（回到"显示已保存值"），旧的测试结果同样失去意义
      setResults({})
      await reload()
      onChanged?.()
    } catch (err) {
      setError(message(err))
    } finally {
      setBusy(false)
    }
  }

  const rebuild = async (name: string) => {
    setRebuilding(name)
    setError(null)
    try {
      const result = await providerApi.rebuildVectors(name)
      setNotice(`「${name}」已重建：重新嵌入了 ${result.embedded} 条切片。`)
      await reload()
      onChanged?.()
    } catch (err) {
      setError(message(err))
    } finally {
      setRebuilding(null)
    }
  }

  const resetAll = async () => {
    setBusy(true)
    try {
      await providerApi.reset()
      setNotice('已清除全部模型配置覆盖，回落到 .env 与默认值')
      setStaleAfterSave([])
      setDraft({})
      await reload()
      onChanged?.()
    } catch (err) {
      setError(message(err))
    } finally {
      setBusy(false)
    }
  }

  const discard = () => {
    setDraft({})
    setNotice('已放弃未保存的改动')
  }

  return {
    view, draft, error, setError, notice, setNotice, busy, testing, results, rebuilding, staleAfterSave,
    byGroup, dirtyCount, set, applyPreset, runTest, save, rebuild, resetAll, discard,
  }
}
