import type { ReactNode } from 'react'
import type { DebugResponse } from '../../types'
import { ArmBadge, Card, Empty, RankBar, armAccent } from '../../components/ui'

const COLUMN_BODY = 'p-0 max-h-[calc(100vh-17rem)] overflow-y-auto'

/** 卡片标题前的步骤号：检索管线从左到右、从上到下就是数据流的顺序。 */
export function StepTitle({ step, children }: { step: number; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span className="grid size-5 place-items-center rounded-md bg-sky-400/15 text-2xs font-semibold text-sky-200 tabular-nums ring-1 ring-sky-400/25 ring-inset">
        {step}
      </span>
      {children}
    </span>
  )
}

function Row({ accent, children }: { accent: string; children: ReactNode }) {
  return <div className={`border-l-2 px-3.5 py-2.5 ${accent}`}>{children}</div>
}

function Title({ children }: { children: ReactNode }) {
  return <div className="truncate text-xs font-medium text-slate-100">{children}</div>
}

function Summary({ children, lines = 2 }: { children: ReactNode; lines?: 2 | 3 }) {
  return (
    <div className={`mt-0.5 text-2xs leading-relaxed text-slate-500 ${lines === 3 ? 'line-clamp-3' : 'line-clamp-2'}`}>
      {children}
    </div>
  )
}

export function DebugColumns({ data }: { data: DebugResponse }) {
  const vectorTotal = data.vector.length
  const lexicalTotal = data.lexical.length
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-4">
      <Card title={<StepTitle step={1}>向量路</StepTitle>} subtitle={`KNN 返回 ${vectorTotal} 条（按余弦距离）`} bodyClassName={COLUMN_BODY}>
        {vectorTotal === 0 ? (
          <Empty compact>向量路没有返回。可能是库内无向量，或全部低于距离下限。</Empty>
        ) : (
          <div className="divide-y divide-white/[0.05]">
            {data.vector.map((item) => (
              <Row key={item.chunk_id} accent={armAccent(item.arm)}>
                <RankBar rank={item.rank} total={vectorTotal} />
                <div className="mt-1.5">
                  <Title>{item.title || '（无标题）'}</Title>
                </div>
                <Summary>{item.summary}</Summary>
                <div className="mt-1.5 flex items-center gap-2">
                  <span className="text-2xs text-slate-400 mono">距离 {item.distance}</span>
                  <ArmBadge arm={item.arm} />
                </div>
              </Row>
            ))}
          </div>
        )}
      </Card>

      <Card title={<StepTitle step={2}>词法路</StepTitle>} subtitle={`FTS5 返回 ${lexicalTotal} 条（按 bm25）`} bodyClassName={COLUMN_BODY}>
        {lexicalTotal === 0 ? (
          <Empty compact>词法路没有命中：查询里没有能在切片文本中匹配到的词项。</Empty>
        ) : (
          <div className="divide-y divide-white/[0.05]">
            {data.lexical.map((item) => (
              <Row key={item.chunk_id} accent="border-l-amber-400/50">
                <RankBar rank={item.rank} total={lexicalTotal} />
                <div className="mt-1.5">
                  <Title>{item.title || '（无标题）'}</Title>
                </div>
                <Summary>{item.summary}</Summary>
                <div className="mt-1.5 text-2xs text-slate-400 mono">bm25 {item.bm25}</div>
              </Row>
            ))}
          </div>
        )}
      </Card>

      <Card title={<StepTitle step={3}>融合池</StepTitle>} subtitle="RRF 后进重排的候选（排名只用名次，不用分数）" bodyClassName={COLUMN_BODY}>
        {data.fusion_pool.length === 0 ? (
          <Empty compact>没有候选。</Empty>
        ) : (
          <div className="divide-y divide-white/[0.05]">
            {data.fusion_pool.map((item) => (
              <Row key={item.chunk_id} accent={armAccent(item.arm)}>
                <Title>{item.title || '（无标题）'}</Title>
                <div className="mt-1.5 flex items-center gap-2">
                  <ArmBadge arm={item.arm} />
                  <span className="text-2xs text-slate-500 mono">
                    向量 {item.vec_rank ?? '—'} / 词法 {item.lex_rank ?? '—'}
                  </span>
                </div>
              </Row>
            ))}
          </div>
        )}
      </Card>

      <Card title={<StepTitle step={4}>最终结果</StepTitle>} subtitle="模型会看到这些（含 score 与是否可回查原文）" bodyClassName={COLUMN_BODY}>
        {data.final.length === 0 ? (
          <Empty compact>没有结果。</Empty>
        ) : (
          <div className="divide-y divide-white/[0.05]">
            {data.final.map((item, index) => (
              <Row key={item.uid} accent={armAccent(item.arm)}>
                <div className="flex min-w-0 items-center gap-1.5">
                  <span className="shrink-0 text-2xs font-semibold text-slate-500 tabular-nums">#{index + 1}</span>
                  <Title>{item.title || '（无标题）'}</Title>
                </div>
                <Summary lines={3}>{item.summary}</Summary>
                <div className="mt-1.5 flex flex-wrap items-center gap-2">
                  <ArmBadge arm={item.arm} />
                  <span className="text-2xs text-slate-400 mono">
                    {item.rerank_score !== null ? `重排 ${item.rerank_score}` : `RRF ${item.rrf_score}`}
                  </span>
                  {item.weight !== 1 && <span className="text-2xs text-violet-300">w={item.weight}</span>}
                  <span className="text-2xs text-slate-600 mono">{item.uid}</span>
                </div>
              </Row>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}
