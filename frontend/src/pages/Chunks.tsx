import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { Chunk } from '../types'
import { Card, Empty, ErrorBox, Input, NoticeBox, Select, Toggle } from '../components/ui'
import { IconLayers, IconSearch } from '../components/icons'
import ChunkDetail from './chunks/ChunkDetail'
import ChunkRow from './chunks/ChunkRow'

type SortKey = 'created_at' | 'hit_count' | 'weight' | 'summary_tokens'

const SORTS: { value: SortKey; label: string }[] = [
  { value: 'created_at', label: '按创建时间' },
  { value: 'hit_count', label: '按命中次数' },
  { value: 'weight', label: '按权重' },
  { value: 'summary_tokens', label: '按摘要长度' },
]

export default function Chunks({
  db,
  onChanged,
  initialUid,
}: {
  db: string
  onChanged: () => void
  /** 从「会话」页的导航点进来时，要直接打开的那条切片。 */
  initialUid?: string | null
}) {
  const [chunks, setChunks] = useState<Chunk[]>([])
  const [total, setTotal] = useState(0)
  const [query, setQuery] = useState('')
  const [includeDeleted, setIncludeDeleted] = useState(false)
  const [sortKey, setSortKey] = useState<SortKey>('created_at')
  const [selected, setSelected] = useState<Chunk | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = useCallback(async () => {
    if (!db) return
    try {
      const result = await api.listChunks({
        db,
        query: query || undefined,
        include_deleted: includeDeleted,
        limit: 300,
      })
      setChunks(result.chunks)
      setTotal(result.total)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [db, query, includeDeleted])

  useEffect(() => {
    void reload()
  }, [reload])

  const sorted = useMemo(() => {
    const copy = [...chunks]
    copy.sort((a, b) => {
      if (sortKey === 'created_at') return b.created_at.localeCompare(a.created_at)
      return Number(b[sortKey] ?? 0) - Number(a[sortKey] ?? 0)
    })
    return copy
  }, [chunks, sortKey])

  const open = async (uid: string) => {
    try {
      setSelected(await api.getChunk(uid, db))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  // 从导航跳进来时自动打开指定切片。依赖里刻意不含 open 本身——
  // 那会让每次列表刷新都把用户手动选中的那条顶掉。
  useEffect(() => {
    if (initialUid) void open(initialUid)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialUid, db])

  const act = async (action: () => Promise<unknown>, message: string, refreshDetail = true) => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await action()
      setNotice(message)
      await reload()
      onChanged()
      if (refreshDetail && selected) await open(selected.chunk_uid)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  if (!db) return <Empty icon={IconLayers}>先在「库管理」里选一个库。</Empty>

  return (
    <div className="space-y-4">
      {error && <ErrorBox onClose={() => setError(null)}>{error}</ErrorBox>}
      {notice && (
        <NoticeBox tone="success" onClose={() => setNotice(null)}>
          {notice}
        </NoticeBox>
      )}

      <div className="flex flex-wrap items-center gap-2.5">
        <div className="w-72">
          <Input
            icon={IconSearch}
            value={query}
            onChange={setQuery}
            placeholder="按摘要/标题/关键词搜索…"
            ariaLabel="搜索切片"
          />
        </div>
        <Select value={sortKey} onChange={setSortKey} options={SORTS} ariaLabel="排序方式" />
        <Toggle checked={includeDeleted} onChange={setIncludeDeleted} label="含已删除" />
        <span className="ml-auto text-2xs text-slate-500 tabular-nums">
          共 {total} 条{chunks.length < total ? `，显示前 ${chunks.length}` : ''}
        </span>
      </div>

      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
        <Card
          icon={IconLayers}
          title="记忆切片"
          subtitle="点开右侧看原文与区间指针"
          className="xl:sticky xl:top-4"
          bodyClassName="p-0 max-h-[calc(100vh-13.5rem)] overflow-y-auto"
        >
          {sorted.length === 0 ? (
            <Empty icon={IconLayers}>
              没有切片。采集对话后执行 <code>duramem summarize</code>，或用 MCP 的 <code>dm_store</code> 手动写入。
            </Empty>
          ) : (
            <div className="divide-y divide-white/[0.05]">
              {sorted.map((chunk) => (
                <ChunkRow
                  key={chunk.chunk_uid}
                  chunk={chunk}
                  selected={selected?.chunk_uid === chunk.chunk_uid}
                  onOpen={() => void open(chunk.chunk_uid)}
                />
              ))}
            </div>
          )}
        </Card>

        {selected ? (
          <ChunkDetail chunk={selected} db={db} busy={busy} onAct={act} onClose={() => setSelected(null)} />
        ) : (
          <Card icon={IconLayers} title="切片详情">
            <Empty icon={IconLayers}>从左边选一条切片，查看摘要、原文与区间指针。</Empty>
          </Card>
        )}
      </div>
    </div>
  )
}
