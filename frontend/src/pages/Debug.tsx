import { useState, type ReactNode } from 'react'
import { api } from '../api'
import type { DebugResponse } from '../types'
import { Badge, Card, Empty, ErrorBox, NoticeBox, Spinner } from '../components/ui'
import { IconFlask, IconInfo, IconSearch } from '../components/icons'
import { DebugColumns } from './debug/Columns'
import { ExpandCard, RankTable, SessionsCard } from './debug/Extras'
import LastCallCard from './debug/LastCallCard'

/**
 * 检索调试面板 —— 这个项目最该有的那一页。
 *
 * 常见的 RAG 工具只给你最终结果。但调参时真正要回答的问题是"这一路由什么贡献"：
 * 词法路有没有召回向量路漏掉的（专有名词、错误码）？融合后名次怎么变的？
 * 不把三路原始排名并列出来，就只能靠猜。
 *
 * 这也是自己实现 RRF 融合、而不是用一体化检索引擎的直接收益——
 * 一体化引擎把融合做在内部，中间排名就是黑盒。
 */
export default function Debug({ db }: { db: string }) {
  const [query, setQuery] = useState('')
  const [data, setData] = useState<DebugResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const run = async () => {
    if (!db || !query.trim()) return
    setLoading(true)
    setError(null)
    try {
      setData(await api.debugSearch(query.trim(), db))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setData(null)
    } finally {
      setLoading(false)
    }
  }

  if (!db) return <Empty icon={IconFlask}>先在「库管理」里选一个库。</Empty>

  return (
    <div className="space-y-4">
      <LastCallCard />

      <div className="dm-card p-3">
        <div className="flex items-center gap-2.5">
          <div className="relative min-w-0 flex-1">
            <IconSearch
              size={16}
              className="pointer-events-none absolute top-1/2 left-3.5 -translate-y-1/2 text-slate-500"
            />
            <input
              value={query}
              aria-label="检索查询"
              placeholder="输入查询，例如错误码、专有名词或一句自然语言…"
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.nativeEvent.isComposing) void run()
              }}
              className="dm-field h-10 rounded-[10px] pl-10 text-[13px]"
            />
          </div>
          <button
            type="button"
            disabled={loading || !query.trim()}
            onClick={() => void run()}
            className="dm-btn dm-btn-primary h-10 rounded-[10px] px-5"
          >
            {loading ? <Spinner label="检索中" /> : <IconSearch size={14} />}
            检索
          </button>
        </div>

        {data && (
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-white/[0.06] px-1 pt-3 text-2xs">
            <div className="flex items-center gap-1.5">
              <Badge tone={data.retrieval_mode.startsWith('hybrid') ? 'emerald' : 'amber'} dot>
                {data.retrieval_mode}
              </Badge>
              {data.reranked && <Badge tone="violet">已重排</Badge>}
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              {Object.entries(data.params).map(([key, value]) => (
                <span key={key} className="mono">
                  <span className="text-slate-500">{key}=</span>
                  <span className="text-slate-300">{value}</span>
                </span>
              ))}
            </div>
            <div className="ml-auto flex items-center gap-3 text-slate-400">
              <Legend color="bg-emerald-400" label="两路都命中" />
              <Legend color="bg-sky-400" label="仅向量" />
              <Legend color="bg-amber-400" label="仅词法" />
            </div>
          </div>
        )}
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}

      {!data && !error && (
        <Card icon={IconInfo} title="怎么用这一页">
          <ul className="space-y-2.5 text-xs leading-relaxed text-slate-400">
            <Tip>
              <span className="text-slate-200">错误码 / 文件路径 / 项目代号</span>{' '}
              这类查询，向量表征很差但词法一次命中。这里能看出最终结果是不是靠词法路救回来的。
            </Tip>
            <Tip>
              标着 <Badge tone="emerald" dot>两路都命中</Badge> 的候选排在前面是 RRF 的设计意图——两路都同意的比单路排第一的更可信。
            </Tip>
            <Tip>
              「两路都命中」很少而结果相关性又低时，别急着调 RRF 参数，先看 <code>向量距离</code>
              ：如果都很接近下限，说明库里可能真没有相关内容。
            </Tip>
          </ul>
        </Card>
      )}

      {data && (
        <>
          {data.warnings.length > 0 && (
            <NoticeBox tone="warn">
              {data.warnings.map((warning) => (
                <div key={warning}>{warning}</div>
              ))}
            </NoticeBox>
          )}
          <DebugColumns data={data} />
          <SessionsCard data={data} />
          <ExpandCard data={data} />
          <RankTable data={data} />
        </>
      )}
    </div>
  )
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`size-2 rounded-full ${color}`} />
      {label}
    </span>
  )
}

function Tip({ children }: { children: ReactNode }) {
  return (
    <li className="flex gap-2.5">
      <span className="mt-[7px] size-1.5 shrink-0 rounded-full bg-sky-400/70" />
      <span>{children}</span>
    </li>
  )
}
