import { api, formatBytes, providerApi } from '../../api'
import type { DatabaseInfo } from '../../types'
import { Badge, Button, Menu, Metric } from '../../components/ui'
import {
  IconArchive,
  IconCamera,
  IconDatabase,
  IconEye,
  IconMinusCircle,
  IconNote,
  IconPencil,
  IconRefresh,
  IconTarget,
  IconTrash,
} from '../../components/icons'
import ColdStartEditor from './ColdStartEditor'

export type Runner = <T>(
  action: () => Promise<T>,
  message: string | ((result: T) => string),
) => Promise<void>

export default function DatabaseRow({
  info,
  browsing,
  isActive,
  busy,
  isOnly,
  fallbackName,
  coldStartOpen,
  onToggleColdStart,
  run,
  onSelect,
  onSetActive,
  onNotice,
}: {
  info: DatabaseInfo
  browsing: boolean
  isActive: boolean
  busy: boolean
  /** 只剩这一个库时不许移除/删除：界面总得有个库可浏览。 */
  isOnly: boolean
  /** 删掉正在浏览的库之后切到哪个库。 */
  fallbackName?: string
  coldStartOpen: boolean
  onToggleColdStart: () => void
  run: Runner
  onSelect: (name: string) => void
  onSetActive: (name: string) => void | Promise<void>
  onNotice: (message: string) => void
}) {
  const rename = () => {
    const next = prompt('新的库名（不影响已有 uid 寻址）', info.name)
    if (next && next !== info.name) {
      void run(() => api.renameDatabase(info.name, next), `已重命名为 ${next}`)
    }
  }

  const unregister = () => {
    if (!confirm(`从注册表移除「${info.name}」？库文件会保留。`)) return
    void run(() => api.deleteDatabase(info.name, false), `已移除 ${info.name}`)
  }

  const purge = () => {
    const typed = prompt(
      `永久删除「${info.name}」及其库文件？不可恢复。\n\n` +
        `文件：${info.file_path}\n` +
        `规模：${info.chunks_alive ?? 0} 条切片 · ${info.messages ?? 0} 条原始消息 · ${formatBytes(info.size_bytes)}\n` +
        `向量来源：${info.vector_source || '未记录'}\n\n` +
        `想留下备份就先用「快照」导出一份。\n` +
        `确认请输入库名「${info.name}」：`,
    )
    if (typed === null) return
    if (typed.trim() !== info.name) {
      onNotice(`库名不匹配，已取消删除「${info.name}」`)
      return
    }
    void run(
      () => api.deleteDatabase(info.name, true),
      (result) =>
        // 只在后端**明确**说没删掉时才告警：还没重启的老服务不会回这个字段，
        // 那时按成功显示，免得把删成功的事说成失败。
        result.file_removed === false
          ? `「${info.name}」已从注册表移除，但文件没能删掉（正被其它进程打开）：` +
            `${result.leftover?.join('、') ?? ''}。` +
            `关掉占用它的进程（serve / MCP 子进程）后再删一次，否则下次扫描会把它收养回来。`
          : `已删除「${info.name}」及其库文件`,
    ).then(() => {
      // 删掉的正好是当前选中的库时，界面不能继续指向一个不存在的库
      if (browsing && fallbackName) onSelect(fallbackName)
    })
  }

  return (
    <div className={`relative ${browsing ? 'bg-sky-400/[0.04]' : 'hover:bg-white/[0.015]'}`}>
      {browsing && (
        <span className="absolute inset-y-0 left-0 w-0.5 bg-linear-to-b from-cyan-300 to-sky-500" />
      )}
      <div className="flex items-start gap-3.5 px-4 py-3.5">
        <span
          className={`mt-0.5 grid size-9 shrink-0 place-items-center rounded-lg ring-1 ring-inset ${
            isActive ? 'bg-violet-400/10 text-violet-300 ring-violet-400/25' : 'bg-slate-800/60 text-slate-400 ring-white/5'
          }`}
        >
          <IconDatabase size={17} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="mr-1 text-sm font-semibold text-slate-50">{info.name}</span>
            {isActive && <Badge tone="violet" dot>记忆库</Badge>}
            {browsing && <Badge tone="sky">正在浏览</Badge>}
            {info.compatible ? (
              <Badge tone="emerald">模型一致</Badge>
            ) : info.vectors_stale ? (
              <Badge tone="rose">向量需重建</Badge>
            ) : (
              <Badge tone="rose">模型不一致</Badge>
            )}
            {info.chunks_alive !== undefined && info.chunks_alive > 150_000 && (
              <Badge tone="amber">接近规模上限</Badge>
            )}
            {info.cold_start?.note && <Badge tone="sky">冷启动留言</Badge>}
          </div>
          <div className="mt-1 truncate text-2xs text-slate-500 mono" title={info.file_path}>
            {info.file_path}
          </div>
          {info.error && <div className="mt-1 text-2xs break-all text-rose-300">{info.error}</div>}
          <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
            <Metric label="切片" value={info.chunks_alive ?? '—'} />
            <Metric label="消息" value={info.messages ?? '—'} />
            <Metric label="向量" value={info.vectors ?? '—'} />
            <Metric label="大小" value={formatBytes(info.size_bytes)} />
            <span
              className="ml-1 text-2xs text-slate-500"
              title="库内向量实际由谁生成，不是配置里写的模型名"
            >
              向量来自 {info.vector_source || '未记录'} · 配置 {info.embedding_model ?? ''}
            </span>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {!info.compatible && info.vectors_stale && (
            <Button
              size="sm"
              variant="primary"
              icon={IconRefresh}
              disabled={busy}
              title="用当前的嵌入模型重算这张库的全部向量"
              onClick={() =>
                void run(
                  () => providerApi.rebuildVectors(info.name),
                  (result) => `「${info.name}」已重建，重新嵌入 ${result.embedded} 条`,
                )
              }
            >
              重建向量
            </Button>
          )}
          <Button
            size="sm"
            variant={isActive ? 'ghost' : 'default'}
            icon={IconTarget}
            disabled={busy || isActive}
            title="设为当前记忆库：模型的检索与写入都跟随它（下一轮检索生效）"
            onClick={() => void onSetActive(info.name)}
          >
            {isActive ? '已是记忆库' : '设为记忆库'}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            icon={IconEye}
            disabled={busy || browsing}
            title={browsing ? '界面正在浏览这个库' : '在界面里浏览这个库（不影响模型读写）'}
            onClick={() => onSelect(info.name)}
          >
            浏览
          </Button>
          <Button
            size="sm"
            variant="ghost"
            icon={IconNote}
            disabled={busy}
            className={coldStartOpen ? 'bg-white/10 text-slate-100' : ''}
            title="每个库可以有自己的开局说明：模型以该库为目标调用 dm_ 工具时随结果附带。配置存在库文件里。"
            onClick={onToggleColdStart}
          >
            冷启动
          </Button>
          <Menu
            label={`更多操作：${info.name}`}
            items={[
              { label: '重命名', description: '只改显示名，库身份由 db_uuid 保证', icon: IconPencil, disabled: busy, onSelect: rename },
              { label: '导出归档', description: '可读 JSON，含会话概览与游标，不含向量', icon: IconArchive, disabled: busy, onSelect: () => api.exportArchive(info.name) },
              { label: '快照', description: 'checkpoint 后安全复制库文件', icon: IconCamera, disabled: busy, onSelect: () => void run(() => api.snapshot(info.name), '已导出快照') },
              'separator',
              { label: '从注册表移除', description: isOnly ? '至少要保留一个库' : '只摘登记，库文件留着', icon: IconMinusCircle, danger: true, disabled: busy || isOnly, onSelect: unregister },
              { label: '删除库文件', description: isOnly ? '至少要保留一个库' : '连同文件永久删除，需输入库名确认', icon: IconTrash, danger: true, disabled: busy || isOnly, onSelect: purge },
            ]}
          />
        </div>
      </div>

      {coldStartOpen && (
        <ColdStartEditor
          info={info}
          busy={busy}
          onSave={(note, enabled) =>
            run(
              () => api.setColdStart(info.name, { note, enabled }),
              `已保存「${info.name}」的冷启动配置`,
            )
          }
          onClose={onToggleColdStart}
        />
      )}
    </div>
  )
}
