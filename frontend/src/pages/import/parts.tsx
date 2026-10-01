import { formatTime } from '../../api'
import type { ImportConversation, ImportReport } from '../../types'
import { Badge, Card, NoticeBox, Stat } from '../../components/ui'
import { IconCheckCircle, IconEye } from '../../components/icons'

export function ImportRow({
  item,
  checked,
  onToggle,
}: {
  item: ImportConversation
  checked: boolean
  onToggle: () => void
}) {
  return (
    <label
      className={`flex cursor-pointer items-start gap-3 px-4 py-3 ${
        checked ? 'bg-sky-400/[0.05]' : 'hover:bg-white/[0.02]'
      } ${item.already_imported ? 'opacity-55' : ''}`}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        aria-label={`选择对话 ${item.title}`}
        className="mt-0.5 size-4 shrink-0 cursor-pointer accent-sky-500"
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="min-w-0 truncate text-xs font-medium text-slate-100">{item.title}</span>
          {item.already_imported && <Badge tone="emerald">已导入</Badge>}
          {item.is_subagent && <Badge tone="amber">子代理</Badge>}
        </div>
        <div className="mt-0.5 line-clamp-1 text-2xs text-slate-500">{item.first_line}</div>
        <div className="mt-1 flex flex-wrap items-center gap-3 text-2xs text-slate-500">
          <span className="tabular-nums">{item.messages} 条</span>
          <span className="tabular-nums">{item.created_at ? formatTime(item.created_at).slice(0, 16) : '—'}</span>
          <span className="truncate mono">{item.session_id}</span>
        </div>
      </div>
    </label>
  )
}

export function ReportCard({ report }: { report: ImportReport }) {
  return (
    <Card
      icon={report.dry_run ? IconEye : IconCheckCircle}
      title={report.dry_run ? '预览结果（没有写入数据）' : '导入结果'}
    >
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="选中对话" value={report.conversations_found} />
        <Stat label="已导入对话" value={report.conversations_imported} />
        <Stat label="导入消息" value={report.messages_imported} />
        <Stat label="跳过（过短）" value={report.skipped_short} />
      </div>
      {report.summarized !== undefined && (
        <p className="mt-3 text-xs text-slate-400">
          已切分 {report.summarized} 个对话，新增 {report.chunks_added} 条记忆切片。
        </p>
      )}
      {report.preview && report.preview.length > 0 && (
        <ul className="dm-panel mt-3 divide-y divide-white/[0.05]">
          {report.preview.slice(0, 12).map((item) => (
            <li key={item.origin_id} className="flex gap-3 px-3.5 py-2 text-2xs">
              <span className="w-14 shrink-0 text-slate-500 tabular-nums">{item.messages} 条</span>
              <span className="truncate text-slate-300">{item.title}</span>
            </li>
          ))}
        </ul>
      )}
      {report.errors && report.errors.length > 0 && (
        <div className="mt-3">
          <NoticeBox tone="warn">
            {report.errors.map((item) => (
              <div key={item}>{item}</div>
            ))}
          </NoticeBox>
        </div>
      )}
    </Card>
  )
}
