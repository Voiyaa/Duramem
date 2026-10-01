import { parseList } from '../../api'
import type { Chunk } from '../../types'
import { Badge } from '../../components/ui'

export default function ChunkRow({
  chunk,
  selected,
  onOpen,
}: {
  chunk: Chunk
  selected: boolean
  onOpen: () => void
}) {
  const tags = parseList(chunk.tags).filter((tag) => tag !== 'manual').slice(0, 2)
  const deleted = chunk.deleted_at !== null
  const superseded = chunk.superseded_by !== null
  return (
    <button
      type="button"
      aria-label={`查看切片 ${chunk.title || chunk.chunk_uid}`}
      aria-current={selected}
      onClick={onOpen}
      className={`relative block w-full px-4 py-3 text-left ${
        selected ? 'bg-sky-400/[0.07]' : 'hover:bg-white/[0.025]'
      } ${deleted || superseded ? 'opacity-55' : ''}`}
    >
      {selected && (
        <span className="absolute inset-y-2 left-0 w-0.5 rounded-r-full bg-linear-to-b from-cyan-300 to-sky-500" />
      )}
      <div className="flex min-w-0 items-center gap-1.5">
        <span className={`min-w-0 truncate text-[13px] font-medium ${selected ? 'text-white' : 'text-slate-100'}`}>
          {chunk.title || '（无标题）'}
        </span>
        {deleted && <Badge tone="rose">已删除</Badge>}
        {superseded && <Badge tone="amber">已被取代</Badge>}
        {chunk.summary_truncated === 1 && <Badge tone="amber">摘要触顶</Badge>}
        {tags.map((tag) => (
          <Badge key={tag} tone="violet">
            {tag}
          </Badge>
        ))}
      </div>
      <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-slate-400">{chunk.summary_text}</p>
      <div className="mt-1.5 flex items-center gap-3 text-2xs text-slate-500 tabular-nums">
        <span className="mono">{chunk.chunk_uid}</span>
        <span>命中 {chunk.hit_count}</span>
        <span>{chunk.summary_tokens} tok</span>
        {chunk.msg_id_start !== null && (
          <span>
            #{chunk.msg_id_start}–{chunk.msg_id_end}
          </span>
        )}
      </div>
    </button>
  )
}
