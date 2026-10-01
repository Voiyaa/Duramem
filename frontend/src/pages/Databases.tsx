import { useCallback, useEffect, useRef, useState } from 'react'
import { api, formatTime } from '../api'
import type { DatabaseInfo, ImportArchiveResult, Stats } from '../types'
import { Button, Card, Empty, ErrorBox, Input, NoticeBox, Stat } from '../components/ui'
import {
  IconCheckCircle,
  IconDatabase,
  IconLayers,
  IconMessages,
  IconPlus,
  IconSparkles,
  IconUpload,
} from '../components/icons'
import DatabaseRow from './databases/DatabaseRow'

export default function Databases({
  db,
  onSelect,
  activeName,
  onSetActive,
  onChanged,
}: {
  db: string
  onSelect: (name: string) => void
  /** 当前记忆库（模型读写的那个）。与 db（界面正在浏览的库）是两件事。 */
  activeName: string | null
  onSetActive: (name: string) => void | Promise<void>
  onChanged: () => void
}) {
  const [infos, setInfos] = useState<DatabaseInfo[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)
  /** 正在展开冷启动编辑器的库名（同一时刻最多展开一个）。 */
  const [coldStartFor, setColdStartFor] = useState<string | null>(null)
  const archiveInput = useRef<HTMLInputElement>(null)

  const reload = useCallback(async () => {
    try {
      const { databases } = await api.listDatabases()
      setInfos(databases)
      setError(null)
      if (db) {
        try {
          setStats(await api.stats(db))
        } catch {
          setStats(null)
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [db])

  useEffect(() => {
    void reload()
  }, [reload])

  const run = async <T,>(action: () => Promise<T>, message: string | ((result: T) => string)) => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await action()
      setNotice(typeof message === 'function' ? message(result) : message)
      await reload()
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const create = () => {
    const name = newName.trim()
    if (!name || busy) return
    void run(() => api.createDatabase(name), `已创建库 ${name}`).then(() => setNewName(''))
  }

  const restoreArchive = (file: File) => {
    const fallback = file.name.replace(/-export\.json$/i, '').replace(/\.json$/i, '') || 'restored'
    const target = prompt('还原为哪个库名？', fallback)
    if (!target || !target.trim()) return
    void run(
      () => api.importArchive(file, { db: target.trim() }),
      (result: ImportArchiveResult) =>
        `已还原为库「${result.db}」：${result.chunks} 条切片` +
        (result.messages ? ` · ${result.messages} 条消息` : '') +
        ` · ${result.layers} 份会话概览` +
        (result.vectors !== undefined ? ` · 重建了 ${result.vectors} 条向量` : '') +
        (result.warnings.length ? `。注意：${result.warnings.join(' ')}` : ''),
    )
  }

  const brief = stats?.brief

  return (
    <div className="space-y-4">
      {error && <ErrorBox onClose={() => setError(null)}>{error}</ErrorBox>}
      {notice && <NoticeBox onClose={() => setNotice(null)}>{notice}</NoticeBox>}

      <Card
        icon={IconDatabase}
        title="记忆库"
        subtitle="一个库 = 一个 SQLite 文件，库与库硬隔离（检索不跨库）。「浏览」只影响界面，「设为记忆库」才决定模型读写哪个库。「更多」菜单里，「从注册表移除」只摘登记、文件留着；「删除库文件」是真删。"
        bodyClassName="p-0"
        right={
          <>
            <div className="w-44">
              <Input value={newName} onChange={setNewName} onEnter={create} placeholder="新库名，如 work" ariaLabel="新库名" />
            </div>
            <Button
              icon={IconUpload}
              disabled={busy}
              title="把 duramem export 生成的归档还原成一个新库。只进新库，不与已有库合并。"
              onClick={() => archiveInput.current?.click()}
            >
              导入归档
            </Button>
            <input
              ref={archiveInput}
              type="file"
              accept=".json,application/json"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (file) restoreArchive(file)
              }}
            />
            <Button variant="primary" icon={IconPlus} disabled={busy || !newName.trim()} onClick={create}>
              新建库
            </Button>
          </>
        }
      >
        {infos.length === 0 ? (
          <Empty icon={IconDatabase}>
            还没有记忆库。建一个，或者用 <code>duramem init</code> 初始化。
          </Empty>
        ) : (
          <div className="divide-y divide-white/[0.06]">
            {infos.map((info) => (
              <DatabaseRow
                key={info.name}
                info={info}
                browsing={info.name === db}
                isActive={info.name === activeName}
                busy={busy}
                isOnly={infos.length <= 1}
                fallbackName={infos.find((item) => item.name !== info.name)?.name}
                coldStartOpen={coldStartFor === info.name}
                onToggleColdStart={() => setColdStartFor((prev) => (prev === info.name ? null : info.name))}
                run={run}
                onSelect={onSelect}
                onSetActive={onSetActive}
                onNotice={setNotice}
              />
            ))}
          </div>
        )}
      </Card>

      {brief && (
        <Card icon={IconSparkles} title={`「${brief.db}」状态`} subtitle="与模型 dm_stats 看到的同一份概况（service.stats_brief 投影）">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat icon={IconLayers} label="有效切片" value={brief.chunks_alive} />
            <Stat icon={IconSparkles} label="向量" value={brief.vectors} hint="与有效切片相等 = 全部可被向量召回" />
            <Stat icon={IconMessages} label="原始消息" value={brief.messages} />
            <Stat
              icon={IconCheckCircle}
              label="已总结会话"
              value={brief.sessions_summarized}
              hint={brief.latest_summary_at ? `最新一批 ${formatTime(brief.latest_summary_at)}` : '还没有总结过'}
            />
          </div>

          <div className="mt-3 space-y-2 empty:hidden">
            {brief.summary_truncated_ratio !== undefined && (
              <NoticeBox tone="warn">
                摘要触顶比例 {(brief.summary_truncated_ratio * 100).toFixed(0)}%——超过 20% 告警线，
                说明 L0 摘要普遍长于标记上限、这个标记已接近失真。在设置页放宽{' '}
                <code>summary_soft_limit_tokens</code> 可让它回到有效水位。
              </NoticeBox>
            )}
            {brief.chunks_missing_vectors !== undefined && (
              <NoticeBox tone="warn">
                有 {brief.chunks_missing_vectors} 条切片没有向量，它们在向量路上召回不到。执行{' '}
                <code>duramem reindex</code> 补齐（若刚换过嵌入模型或改过 VECTOR_CHUNK_SIZE，改用{' '}
                <code>duramem rebuild-vectors</code>）。
              </NoticeBox>
            )}
            {brief.vectors_stale && (
              <NoticeBox tone="warn">
                库内向量与当前嵌入模型不一致，需要重建向量后向量路才可靠（列表行的「重建向量」按钮）。
              </NoticeBox>
            )}
            {brief.warnings?.map((warning) => (
              <NoticeBox key={warning} tone="warn">
                {warning}
              </NoticeBox>
            ))}
          </div>
        </Card>
      )}
    </div>
  )
}
