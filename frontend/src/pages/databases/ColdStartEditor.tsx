import { useState } from 'react'
import type { DatabaseInfo } from '../../types'
import { Button, TextArea, Toggle } from '../../components/ui'
import { IconCheck } from '../../components/icons'

export default function ColdStartEditor({
  info,
  busy,
  onSave,
  onClose,
}: {
  info: DatabaseInfo
  busy: boolean
  onSave: (note: string, enabled: boolean) => void | Promise<void>
  onClose: () => void
}) {
  const [note, setNote] = useState(info.cold_start?.note ?? '')
  const [enabled, setEnabled] = useState(info.cold_start?.enabled ?? true)

  return (
    <div className="mr-4 mb-4 ml-[4.125rem] animate-fade-up space-y-2.5 rounded-xl border border-white/[0.07] bg-ink-950/50 p-3.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-4">
          <span className="text-xs font-medium text-slate-200">开局说明</span>
          <Toggle checked={enabled} onChange={setEnabled} label="注入开关" disabled={busy} />
        </div>
        <div className="flex shrink-0 gap-1.5">
          <Button size="sm" variant="ghost" disabled={busy} onClick={onClose}>
            收起
          </Button>
          <Button
            size="sm"
            variant="primary"
            icon={IconCheck}
            disabled={busy}
            onClick={() => void onSave(note, enabled)}
          >
            应用
          </Button>
        </div>
      </div>
      <TextArea rows={4} value={note} disabled={busy} placeholder="留空 = 该库不注入" onChange={setNote} />
      <p className="text-2xs leading-relaxed text-slate-500">
        模型以<strong className="font-medium text-slate-300">该库</strong>为目标调用 dm_ 工具时随结果附带这段说明。
        打开开关或修改留言后，下一次调用注入一次、之后静默；想再注入就重拨开关或改一下留言。
        配置存在库文件内部（db_meta），随快照与归档导出一起走。
      </p>
    </div>
  )
}
