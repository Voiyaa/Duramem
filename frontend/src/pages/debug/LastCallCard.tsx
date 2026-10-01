import { useEffect, useState } from 'react'
import { api, formatTime } from '../../api'
import type { McpLastCall } from '../../types'
import { Badge, Button, Card, Empty, ErrorBox } from '../../components/ui'
import { IconChevronRight, IconRefresh, IconTerminal } from '../../components/icons'

export default function LastCallCard() {
  const [data, setData] = useState<McpLastCall | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)

  const reload = async () => {
    try {
      setData(await api.lastMcpCall())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  return (
    <Card
      icon={IconTerminal}
      title="最近一次 MCP 调用"
      subtitle="宿主模型真实发出的工具调用：输入参数与它看到的完整输出（含冷启动留言等附带的字段）"
      right={
        <Button size="sm" variant="ghost" icon={IconRefresh} onClick={() => void reload()}>
          刷新
        </Button>
      }
    >
      {error && <ErrorBox>{error}</ErrorBox>}
      {!data || !data.exists ? (
        <Empty compact>
          还没有记录。宿主模型调用一次 dm_ 工具（比如 dm_search）后，这里就会出现那次调用的快照。
        </Empty>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-2xs text-slate-400">
            <Badge tone="violet">{data.tool}</Badge>
            {data.db && <Badge tone="sky">{data.db}</Badge>}
            {data.cold_start_injected && <Badge tone="emerald">已附带冷启动留言</Badge>}
            {data.duration_ms !== undefined && <span className="tabular-nums">{data.duration_ms} ms</span>}
            {data.ts && <span>{formatTime(data.ts)}</span>}
            {data.pid !== undefined && (
              <span className="text-slate-500" title="写这条快照的 MCP 子进程">
                pid {data.pid}
              </span>
            )}
            <button
              type="button"
              aria-expanded={expanded}
              onClick={() => setExpanded((prev) => !prev)}
              className="ml-auto inline-flex items-center gap-1 text-sky-300 hover:text-sky-200"
            >
              <IconChevronRight size={13} className={`transition-transform ${expanded ? 'rotate-90' : ''}`} />
              {expanded ? '收起输入/输出' : '展开输入/输出'}
            </button>
          </div>
          {expanded && (
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              <JsonBlock label="输入参数" value={data.arguments ?? {}} />
              <JsonBlock label="返回输出（模型看到的）" value={data.output ?? {}} />
            </div>
          )}
        </div>
      )}
    </Card>
  )
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <div className="mb-1.5 text-2xs font-medium text-slate-400">{label}</div>
      <pre className="dm-panel max-h-96 overflow-auto p-3 text-2xs leading-relaxed break-all whitespace-pre-wrap text-slate-300 mono">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  )
}
