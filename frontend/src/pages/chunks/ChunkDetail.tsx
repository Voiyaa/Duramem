import { useEffect, useId, useState } from 'react'
import { api, parseList } from '../../api'
import type { Chunk } from '../../types'
import { Badge, Button, Card, FieldLabel, Input, TextArea } from '../../components/ui'
import { IconCheck, IconLayers, IconSparkles } from '../../components/icons'
import DetailActions, { type Act } from './DetailActions'
import { InternalFields, MetaGrid, Neighbours, OriginalPanel } from './parts'

const split = (value: string) =>
  value
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)

export default function ChunkDetail({
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
  const id = useId()
  const [title, setTitle] = useState(chunk.title)
  const [l0, setL0] = useState(chunk.summary_text)
  const [keywords, setKeywords] = useState(parseList(chunk.keywords).join(', '))
  const [tags, setTags] = useState(parseList(chunk.tags).join(', '))
  const [weight, setWeight] = useState(String(chunk.weight))
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    setTitle(chunk.title)
    setL0(chunk.summary_text)
    setKeywords(parseList(chunk.keywords).join(', '))
    setTags(parseList(chunk.tags).join(', '))
    setWeight(String(chunk.weight))
    setDirty(false)
  }, [chunk])

  const edit = (setter: (value: string) => void) => (value: string) => {
    setter(value)
    setDirty(true)
  }

  const save = () =>
    onAct(
      () =>
        api.patchChunk(chunk.chunk_uid, db, {
          title,
          summary_text: l0,
          keywords: split(keywords),
          tags: split(tags),
          weight: Number(weight) || 1,
        }),
      '已保存。改到摘要/标题/关键词时会自动重建分词与向量。',
    ).then(() => setDirty(false))

  return (
    <Card
      icon={IconLayers}
      title={chunk.title || '（无标题）'}
      subtitle={
        <>
          uid <span className="mono text-slate-400">{chunk.chunk_uid}</span> · 这是对外句柄，改名不影响它
        </>
      }
      right={<DetailActions chunk={chunk} db={db} busy={busy} onAct={onAct} onClose={onClose} />}
    >
      <div className="space-y-5">
        <MetaGrid chunk={chunk} />

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="space-y-1.5">
            <FieldLabel
              htmlFor={`${id}-l0`}
              hint="注入上下文的就是它"
              right={chunk.summary_truncated === 1 ? <Badge tone="amber">已触顶</Badge> : undefined}
            >
              L0 摘要
            </FieldLabel>
            <TextArea id={`${id}-l0`} value={l0} onChange={edit(setL0)} rows={6} />
            <p className="text-2xs leading-relaxed text-slate-500">
              改动会重建 FTS5 分词与向量（embedding 输入 = 摘要 + 标题 + 关键词 + 标签）
            </p>
          </div>
          <OriginalPanel chunk={chunk} />
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="space-y-1.5">
            <FieldLabel htmlFor={`${id}-title`} hint="你的命名权">
              标题
            </FieldLabel>
            <Input id={`${id}-title`} value={title} onChange={edit(setTitle)} />
            {chunk.title_suggested && chunk.title_suggested !== chunk.title && (
              <button
                type="button"
                onClick={() => edit(setTitle)(chunk.title_suggested ?? '')}
                className="inline-flex max-w-full items-center gap-1 text-2xs text-sky-300 hover:text-sky-200"
              >
                <IconSparkles size={12} className="shrink-0" />
                <span className="truncate">采纳模型建议：{chunk.title_suggested}</span>
              </button>
            )}
          </div>
          <div className="space-y-1.5">
            <FieldLabel htmlFor={`${id}-kw`} hint="参与向量，不占上下文">
              关键词
            </FieldLabel>
            <Input id={`${id}-kw`} value={keywords} onChange={edit(setKeywords)} placeholder="逗号或空格分隔" />
          </div>
          <div className="space-y-1.5">
            <FieldLabel htmlFor={`${id}-tags`} hint="tags 可过滤 · weight 参与排序">
              标签 / 权重
            </FieldLabel>
            <div className="flex gap-2">
              <Input id={`${id}-tags`} value={tags} onChange={edit(setTags)} />
              <input
                value={weight}
                aria-label="权重"
                inputMode="decimal"
                onChange={(event) => edit(setWeight)(event.target.value)}
                className="dm-field w-16 shrink-0 text-center tabular-nums"
              />
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3 border-t border-white/[0.06] pt-4">
          <Button variant="primary" icon={IconCheck} disabled={busy || !dirty} onClick={() => void save()}>
            保存修改
          </Button>
          {dirty ? (
            <span className="inline-flex items-center gap-1.5 text-2xs text-amber-300">
              <span className="size-1.5 rounded-full bg-amber-400" />
              有未保存的修改
            </span>
          ) : (
            <span className="text-2xs text-slate-500">没有改动</span>
          )}
        </div>

        <Neighbours items={chunk.neighbours} />
        <InternalFields chunk={chunk} />
      </div>
    </Card>
  )
}
