import type { DebugResponse } from '../../types'
import { ArmBadge, Badge, Card, Empty } from '../../components/ui'
import { IconGraph, IconMessages } from '../../components/icons'
import { StepTitle } from './Columns'

const dash = <span className="text-slate-600">—</span>

export function SessionsCard({ data }: { data: DebugResponse }) {
  const sessions = data.sessions ?? []
  const hasArms = (data.session_vector?.length ?? 0) > 0 || (data.session_lexical?.length ?? 0) > 0
  return (
    <Card
      icon={IconMessages}
      title={<StepTitle step={5}>会话路（容器层）</StepTitle>}
      subtitle="切片召回要求查询里有词；会话概览进索引后，没有关键词的查询也能命中会话"
    >
      {sessions.length === 0 ? (
        <Empty compact>
          这次没有命中会话。可能是还没有生成会话概览（见「会话」页的按策略刷新），或者查询与任何会话摘要都不够接近。
        </Empty>
      ) : (
        <div className="grid grid-cols-1 gap-2.5 lg:grid-cols-2">
          {sessions.map((item, index) => (
            <div key={item.session_id} className="dm-panel px-3.5 py-3">
              <div className="flex items-center gap-2">
                <span className="text-2xs font-semibold text-slate-500 tabular-nums">#{index + 1}</span>
                <span className="min-w-0 truncate text-2xs text-slate-300 mono">{item.session_id}</span>
                <Badge>{item.slice_count} 条切片</Badge>
                <span className="ml-auto text-2xs text-slate-400 mono">{item.score}</span>
              </div>
              <div className="mt-1.5 line-clamp-3 text-2xs leading-relaxed text-slate-500">{item.abstract}</div>
            </div>
          ))}
        </div>
      )}
      {hasArms && (
        <div className="mt-3 text-2xs text-slate-500">
          会话向量路 {data.session_vector?.length ?? 0} 条命中 · 会话词法路 {data.session_lexical?.length ?? 0} 条命中 ·
          融合池 {data.session_fusion_pool?.length ?? 0} 条
        </div>
      )}
    </Card>
  )
}

export function ExpandCard({ data }: { data: DebugResponse }) {
  const expand = data.expand
  return (
    <Card
      icon={IconGraph}
      title={<StepTitle step={6}>图扩散（迭代检索环路）</StepTitle>}
      subtitle="从直接命中沿链接图做 PPR，把多跳相关、查询词够不着的切片增补进来（增补不替换）"
    >
      {!expand || !expand.triggered ? (
        <Empty compact>
          这次没有触发扩散。触发信号：产出不足（命中数 &lt; top_k）、弱结果告警、重排分平坦——信号全暗时不会为扩散花钱。可在「设置」页调整。
        </Empty>
      ) : expand.error ? (
        <Empty compact>扩散执行失败：{expand.error}</Empty>
      ) : (expand.emitted?.length ?? 0) === 0 ? (
        <Empty compact>
          触发原因：{expand.reason ?? '—'}；但种子没有任何可用链接（图尚未生成时可在库管理执行链接重建，等价命令 links-rebuild）。
        </Empty>
      ) : (
        <div className="space-y-2.5">
          <div className="text-2xs text-slate-500">
            触发原因 <span className="text-slate-300 mono">{expand.reason}</span> · 种子 {expand.seeds?.length ?? 0} 条 ·
            增补 {expand.emitted?.length ?? 0} 条（origin=expand，不占直接命中的名次）
          </div>
          {expand.emitted!.map((item) => (
            <div key={item.uid} className="dm-panel px-3.5 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="max-w-[24rem] truncate text-xs font-medium text-slate-100">
                  {data.expanded?.find((hit) => hit.uid === item.uid)?.title || item.uid}
                </span>
                <Badge tone="violet">
                  {item.relation} · {item.hops} 跳
                </Badge>
                <span className="ml-auto text-2xs text-slate-400 mono">PPR {item.ppr}</span>
              </div>
              <div className="mt-1 text-2xs text-slate-500">
                经由种子 <span className="mono">{item.via_seed ?? '—'}</span> · 路径{' '}
                <span className="mono">{item.path.join(' → ')}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

const HEADERS = ['切片', '向量名次', '向量距离', '词法名次', 'bm25', '重排名次', '命中情况']

export function RankTable({ data }: { data: DebugResponse }) {
  return (
    <Card title="名次变化对照" subtitle="同一条候选在三路里的位置" bodyClassName="p-0">
      <div className="overflow-x-auto">
        <table className="w-full text-2xs">
          <thead>
            <tr className="border-b border-white/[0.06] bg-white/[0.02] text-left text-slate-500">
              {HEADERS.map((header) => (
                <th key={header} className="px-4 py-2.5 font-medium whitespace-nowrap">
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-white/[0.04]">
            {data.final.map((item) => (
              <tr key={item.uid} className="text-slate-300 hover:bg-white/[0.02]">
                <td className="max-w-[20rem] truncate px-4 py-2.5 text-slate-100">{item.title || '（无标题）'}</td>
                <td className="px-4 py-2.5 tabular-nums">{item.vec_rank ?? dash}</td>
                <td className="px-4 py-2.5 tabular-nums mono">{item.vec_distance ?? dash}</td>
                <td className="px-4 py-2.5 tabular-nums">{item.lex_rank ?? dash}</td>
                <td className="px-4 py-2.5 tabular-nums mono">{item.lex_score ?? dash}</td>
                <td className="px-4 py-2.5 tabular-nums">{item.rerank_rank ?? dash}</td>
                <td className="px-4 py-2.5">
                  <ArmBadge arm={item.arm} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
