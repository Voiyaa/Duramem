import ReactMarkdown from 'react-markdown'
import { formatTime } from '../../api'
import type { Chunk } from '../../types'
import { Empty, FieldLabel, KeyValue } from '../../components/ui'
import {
  IconChevronRight,
  IconClock,
  IconHash,
  IconLink,
  IconMessages,
  IconTrending,
  type IconType,
} from '../../components/icons'

function Meta({ icon: Icon, label, value, hint }: { icon: IconType; label: string; value: string; hint?: string }) {
  return (
    <div title={hint} className="dm-panel px-3 py-2.5">
      <div className="flex items-center gap-1.5 text-2xs text-slate-500">
        <Icon size={12} className="shrink-0" />
        {label}
      </div>
      <div className="mt-1 truncate text-xs font-medium text-slate-100 tabular-nums" title={value}>
        {value}
      </div>
    </div>
  )
}

export function MetaGrid({ chunk }: { chunk: Chunk }) {
  return (
    <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
      <Meta icon={IconClock} label="创建时间" value={formatTime(chunk.created_at)} />
      <Meta icon={IconTrending} label="命中次数" value={String(chunk.hit_count)} hint="仅展示，不参与排序" />
      <Meta icon={IconHash} label="摘要长度" value={`${chunk.summary_tokens} tok`} />
      <Meta
        icon={IconMessages}
        label="消息区间"
        value={chunk.msg_id_start !== null ? `#${chunk.msg_id_start}–${chunk.msg_id_end}` : '无（会话级）'}
      />
    </div>
  )
}

export function OriginalPanel({ chunk }: { chunk: Chunk }) {
  return (
    <div className="space-y-1.5">
      <FieldLabel hint="模型回查时看到的逐字内容">L2 原文</FieldLabel>
      {chunk.original_text ? (
        <div className="dm-panel max-h-64 overflow-y-auto px-3.5 py-3 text-xs">
          <div className="markdown-body">
            <ReactMarkdown>{chunk.original_text}</ReactMarkdown>
          </div>
        </div>
      ) : (
        <div className="dm-panel">
          <Empty compact>这条切片没有关联原文（手动写入的切片没有）。</Empty>
        </div>
      )}
      {chunk.original_char_start !== null && chunk.original_char_end !== null && (
        <p className="text-2xs text-slate-500 mono">
          字符区间 [{chunk.original_char_start}, {chunk.original_char_end})，长度{' '}
          {chunk.original_char_end - chunk.original_char_start}
        </p>
      )}
      {chunk.source_session && (
        <p className="text-2xs text-slate-500">
          所属会话 <span className="mono text-slate-400">{chunk.source_session}</span> — 它的概览（L1）在「会话」页
        </p>
      )}
    </div>
  )
}

export function Neighbours({ items }: { items: Chunk['neighbours'] }) {
  if (!items || items.length === 0) return null
  return (
    <div className="space-y-2">
      <FieldLabel hint="模型可用 dm_read_neighbors 顺着上下文走">相邻切片</FieldLabel>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        {items.map((item) => (
          <div key={item.uid} className="dm-panel flex items-start gap-2.5 px-3 py-2.5">
            <IconLink size={13} className="mt-0.5 shrink-0 text-slate-500" />
            <div className="min-w-0">
              <div className="truncate text-xs text-slate-200">{item.title || '（无标题）'}</div>
              <div className="mt-0.5 line-clamp-1 text-2xs text-slate-500">{item.summary_text}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export function InternalFields({ chunk }: { chunk: Chunk }) {
  return (
    <details className="group rounded-xl border border-white/[0.06] bg-white/[0.015]">
      <summary className="flex items-center gap-1.5 rounded-xl px-3.5 py-2.5 text-2xs font-medium text-slate-400 hover:text-slate-200">
        <IconChevronRight size={13} className="transition-transform group-open:rotate-90" />
        内部字段
      </summary>
      <div className="border-t border-white/[0.06] px-3.5 py-3">
        <KeyValue
          items={[
            { label: 'content_hash', value: <span className="mono">{chunk.content_hash.slice(0, 24)}…</span> },
            { label: '来源窗口', value: chunk.source_window ?? '—' },
            { label: '来源会话', value: chunk.source_session ?? '—' },
            { label: '来源库', value: chunk.source_db ?? '—' },
            { label: 'updated_at', value: formatTime(chunk.updated_at) },
            { label: 'deleted_at', value: chunk.deleted_at ? formatTime(chunk.deleted_at) : '—' },
            { label: 'superseded_by', value: chunk.superseded_by ?? '—' },
          ]}
        />
      </div>
    </details>
  )
}
