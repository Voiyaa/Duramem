import { api } from '../../api'
import type { Chunk } from '../../types'
import { Button } from '../../components/ui'
import { IconMinusCircle, IconTrash, IconUndo, IconX } from '../../components/icons'

export type Act = (action: () => Promise<unknown>, message: string) => Promise<void>

/** 详情卡右上角：软删是可恢复的日常操作（普通按钮），真删才用危险色。 */
export default function DetailActions({
  chunk,
  db,
  busy,
  onAct,
  onClose,
}: {
  chunk: Chunk
  db: string
  busy: boolean
  onAct: Act
  onClose: () => void
}) {
  const hardDelete = () => {
    const ok = confirm(
      [
        '真删除这条记忆切片？不可恢复。',
        '（这是「当作从没记过」，不是软删除。）',
        '',
        `标题：${chunk.title || '（无标题）'}`,
        `uid：${chunk.chunk_uid}`,
        `消息区间：${chunk.msg_id_start ?? '—'}–${chunk.msg_id_end ?? '—'}`,
        '',
        '原文（messages）不会被删，重新总结可以再生成切片；',
        '但总结游标不会回退，所以删了之后不会自动重建这一段。',
      ].join('\n'),
    )
    if (!ok) return
    void onAct(() => api.deleteChunk(chunk.chunk_uid, db, true), '已真删除（不可恢复）')
  }

  return (
    <>
      {chunk.deleted_at ? (
        <Button
          size="sm"
          variant="primary"
          icon={IconUndo}
          disabled={busy}
          onClick={() => void onAct(() => api.restoreChunk(chunk.chunk_uid, db), '已恢复')}
        >
          恢复
        </Button>
      ) : (
        <Button
          size="sm"
          icon={IconMinusCircle}
          disabled={busy}
          title="软删除，可恢复。向量保留，恢复后无需重新嵌入。"
          onClick={() => void onAct(() => api.deleteChunk(chunk.chunk_uid, db), '已软删除，可恢复')}
        >
          软删除
        </Button>
      )}
      <Button
        size="sm"
        variant="danger"
        icon={IconTrash}
        disabled={busy}
        title="真删除：连向量与索引一起清掉，不可恢复。原文不受影响。"
        onClick={hardDelete}
      >
        真删除
      </Button>
      <span className="mx-1 h-4 w-px bg-white/10" aria-hidden="true" />
      <Button size="sm" variant="ghost" icon={IconX} iconOnly title="收起详情" onClick={onClose} />
    </>
  )
}
