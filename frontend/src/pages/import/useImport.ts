import { useCallback, useEffect, useMemo, useState } from 'react'
import { importApi } from '../../api'
import type { ImportDefault, ImportListing, ImportReport, ImportSourceInfo } from '../../types'

/** 拿不到 /api/import/sources 时的兜底。必须用规范名：后端也认 zcode/rollout/claude 这类别名，
    但 defaults[].source 只用规范名，用别名就对不上默认路径，界面会误报「本机没找到」。 */
const FALLBACK_SOURCES: ImportSourceInfo[] = [
  { name: 'zcode-db', description: 'ZCode 会话库' },
  { name: 'zcode-rollout', description: 'ZCode 模型往返日志' },
  { name: 'claude-code', description: 'Claude Code 转录' },
  { name: 'dsh', description: 'DeepSeek Harness 会话' },
  { name: 'generic', description: '通用 JSON / JSONL' },
]

/** 导入页的全部状态与动作；页面组件只管摆放。 */
export function useImport(db: string, onChanged: () => void) {
  const [sources, setSources] = useState<ImportSourceInfo[]>(FALLBACK_SOURCES)
  const [defaults, setDefaults] = useState<ImportDefault[]>([])
  const [source, setSource] = useState('zcode-db')
  const [path, setPath] = useState('')
  const [listing, setListing] = useState<ImportListing | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [includeSubagents, setIncludeSubagents] = useState(false)
  const [onlyPending, setOnlyPending] = useState(true)
  const [search, setSearch] = useState('')
  const [summarize, setSummarize] = useState(true)
  const [maxMessages, setMaxMessages] = useState('')
  const [report, setReport] = useState<ImportReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    void (async () => {
      try {
        const info = await importApi.sources()
        if (info.sources.length > 0) setSources(info.sources)
        setDefaults(info.defaults)
        // 默认挑一个本机存在的源，省得用户先去找路径
        const usable = info.defaults.find((item) => item.exists)
        if (usable) setSource(usable.source)
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      }
    })()
  }, [])

  const activeDefault = useMemo(() => defaults.find((item) => item.source === source), [defaults, source])

  const load = useCallback(
    async (refresh = false) => {
      setLoading(true)
      setError(null)
      try {
        const result = await importApi.list({
          source,
          path: path.trim() || undefined,
          db,
          include_subagents: includeSubagents,
          refresh,
        })
        setListing(result)
        setSelected(new Set())
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
        setListing(null)
      } finally {
        setLoading(false)
      }
    },
    [source, path, db, includeSubagents],
  )

  const visible = useMemo(() => {
    if (!listing) return []
    const keyword = search.trim().toLowerCase()
    return listing.conversations.filter((item) => {
      if (onlyPending && item.already_imported) return false
      if (!keyword) return true
      return (
        item.title.toLowerCase().includes(keyword) ||
        item.first_line.toLowerCase().includes(keyword) ||
        item.session_id.toLowerCase().includes(keyword)
      )
    })
  }, [listing, onlyPending, search])

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleAll = (on: boolean) => setSelected(on ? new Set(visible.map((item) => item.session_id)) : new Set())

  const run = async (dryRun: boolean, only?: string[]) => {
    const targets = only ?? [...selected]
    if (targets.length === 0) return
    setBusy(true)
    setError(null)
    setReport(null)
    try {
      const result = await importApi.run({
        source,
        path: path.trim() || undefined,
        db,
        sessions: targets,
        summarize: dryRun ? false : summarize,
        dry_run: dryRun,
        max_messages: maxMessages.trim() ? Number(maxMessages) : undefined,
        include_subagents: includeSubagents,
      })
      setReport(result)
      if (!dryRun && result.conversations_imported > 0) {
        onChanged()
        await load(true)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  /** 当前筛选下还没导入的那些——一键导入的作用域就是它们。 */
  const pendingVisible = useMemo(() => visible.filter((item) => !item.already_imported), [visible])
  const pendingMessages = useMemo(() => pendingVisible.reduce((sum, item) => sum + item.messages, 0), [pendingVisible])
  const totalMessages = useMemo(
    () => visible.filter((item) => selected.has(item.session_id)).reduce((sum, item) => sum + item.messages, 0),
    [visible, selected],
  )

  const importAllPending = () => {
    if (pendingVisible.length === 0) return
    const skipped = visible.length - pendingVisible.length
    const ok = confirm(
      `把当前筛选下 ${pendingVisible.length} 个未导入的对话一次导入「${db}」？\n\n` +
        `约 ${pendingMessages} 条对话消息（系统提醒与过程叙述不计）` +
        (skipped > 0 ? `\n另有 ${skipped} 个已导入的会跳过` : '') +
        `\n${summarize ? '导入后切成记忆切片' : '只入库原文，不切片'}` +
        (maxMessages.trim() ? `\n每个对话只取最近 ${maxMessages.trim()} 条` : '') +
        `\n\n导入是幂等的：同一个对话反复导入不会产生副本。\n` +
        `想缩小范围就先改上面的筛选或搜索，再点这个按钮。`,
    )
    if (!ok) return
    const targets = pendingVisible.map((item) => item.session_id)
    setSelected(new Set(targets))
    void run(false, targets)
  }

  return {
    sources, defaults, source, setSource, path, setPath, listing, selected,
    includeSubagents, setIncludeSubagents, onlyPending, setOnlyPending, search, setSearch,
    summarize, setSummarize, maxMessages, setMaxMessages, report, error, setError, loading, busy,
    activeDefault, load, visible, toggle, toggleAll, run,
    pendingVisible, pendingMessages, totalMessages, importAllPending,
  }
}
