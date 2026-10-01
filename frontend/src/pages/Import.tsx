import { Button, Card, Empty, ErrorBox, Input, Segmented, Spinner, Stat, Toggle } from '../components/ui'
import {
  IconCheck,
  IconDownload,
  IconEye,
  IconInbox,
  IconLayers,
  IconMessages,
  IconRefresh,
  IconSearch,
  IconSliders,
} from '../components/icons'
import { ImportRow, ReportCard } from './import/parts'
import { useImport } from './import/useImport'

/**
 * 历史对话导入 —— 先列出来、挑、再导入。
 *
 * 有一条曾经的硬规定是"不做一键导入全部"，理由是历史里大部分对话与当前无关，
 * 全灌进记忆库只会让检索变差。那条理由**依然成立**，但它防的是"不看就灌"，
 * 不是"批量灌"。现在提供一键导入，同时保留三道护栏：
 *
 *  1. 范围 = **当前筛选**（列表与筛选仍然是"挑"的那个界面，不越过它去全库扫）；
 *  2. 执行前把"将导入几个对话、多少条消息、写进哪个库"摆出来要求确认；
 *  3. 后端仍然要求显式传会话列表（空列表直接拒绝），没有"什么都不传就全灌"的路径。
 *
 * 之所以要这条路：hook 自动采集已经下线（载荷残缺、与导入共用键空间会互相覆盖、
 * 每轮约 0.9 秒、数据质量还低于导入），"零操作"的位置空了，不该让用户用 N 次
 * 点击去补。见设计文档 A.8 的决策变更。
 */
export default function Import({ db, onChanged }: { db: string; onChanged: () => void }) {
  const {
    sources, defaults, source, setSource, path, setPath, listing, selected,
    includeSubagents, setIncludeSubagents, onlyPending, setOnlyPending, search, setSearch,
    summarize, setSummarize, maxMessages, setMaxMessages, report, error, setError, loading, busy,
    activeDefault, load, visible, toggle, toggleAll, run,
    pendingVisible, pendingMessages, totalMessages, importAllPending,
  } = useImport(db, onChanged)

  const sourceOptions = sources.map(({ name, description }) => {
    const found = defaults.find((item) => item.source === name)?.exists
    return {
      value: name,
      title: `${description} · ${found ? '本机找到了默认位置' : '默认位置没找到，需要手动填路径'}`,
      label: (
        <>
          <span className={`size-1.5 rounded-full ${found ? 'bg-emerald-400' : 'bg-slate-600'}`} />
          {name}
        </>
      ),
    }
  })

  return (
    <div className="space-y-4">
      {error && <ErrorBox onClose={() => setError(null)}>{error}</ErrorBox>}

      <Card icon={IconInbox} title="选择来源" subtitle="默认导出的是宿主自己的会话记录。导入只写进 Duramem，源文件全程只读。">
        <div className="space-y-3.5">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1.5">
              <div className="text-2xs font-medium text-slate-300">来源</div>
              <Segmented value={source} onChange={setSource} options={sourceOptions} ariaLabel="导入来源" />
            </div>
            <label className="min-w-[16rem] flex-1 space-y-1.5">
              <span className="block text-2xs font-medium text-slate-300">
                路径 <span className="font-normal text-slate-500">（留空用默认位置）</span>
              </span>
              <Input value={path} onChange={setPath} placeholder={activeDefault?.path ?? ''} />
            </label>
            <Button variant="primary" icon={loading ? undefined : IconSearch} disabled={loading} onClick={() => void load(false)}>
              {loading && <Spinner label="读取中" />}
              列出对话
            </Button>
            <Button variant="ghost" icon={IconRefresh} disabled={loading} onClick={() => void load(true)}>
              刷新
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-2xs">
            <Toggle checked={includeSubagents} onChange={setIncludeSubagents} label="含子代理会话" />
            <Toggle checked={onlyPending} onChange={setOnlyPending} label="只看未导入" />
            {activeDefault && (
              <span className={`min-w-0 truncate ${activeDefault.exists ? 'text-slate-500' : 'text-amber-300'}`}>
                {activeDefault.label}：
                {activeDefault.exists ? <span className="mono">{activeDefault.path}</span> : '未找到，需手动填路径'}
                {activeDefault.hint ? ` · ${activeDefault.hint}` : ''}
              </span>
            )}
          </div>
        </div>
      </Card>

      {listing && (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat icon={IconMessages} label="可选对话" value={listing.total} hint="不含子代理时为真实对话数" />
            <Stat icon={IconEye} label="当前显示" value={visible.length} hint="受搜索与「只看未导入」筛选" />
            <Stat icon={IconCheck} label="已勾选" value={selected.size} />
            <Stat icon={IconLayers} label="将导入消息" value={totalMessages} hint="勾选对话的对话消息总数（系统提醒与过程叙述不计）" />
          </div>

          <Card
            icon={IconMessages}
            title="挑要导入的对话"
            subtitle={
              <>
                共 {listing.total} 个对话，勾选后导入。一键导入的作用域是
                <strong className="font-medium text-slate-300">当前筛选下未导入的那些</strong>，点之前先看筛选对不对
              </>
            }
            bodyClassName="p-0"
            right={
              <>
                <Button size="sm" variant="ghost" onClick={() => toggleAll(true)}>
                  全选当前
                </Button>
                <Button size="sm" variant="ghost" onClick={() => toggleAll(false)}>
                  清空
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  icon={busy ? undefined : IconDownload}
                  disabled={busy || pendingVisible.length === 0}
                  title={`把当前筛选下 ${pendingVisible.length} 个未导入的对话（约 ${pendingMessages} 条消息）一次导入，执行前会再确认一次`}
                  onClick={importAllPending}
                >
                  {busy && <Spinner label="导入中" />}
                  一键导入未导入（{pendingVisible.length}）
                </Button>
              </>
            }
          >
            <div className="border-b border-white/[0.06] px-4 py-3">
              <div className="w-full max-w-sm">
                <Input icon={IconSearch} value={search} onChange={setSearch} placeholder="按标题、首行或会话 id 过滤…" ariaLabel="过滤对话" />
              </div>
            </div>
            {visible.length === 0 ? (
              <Empty compact>
                {listing.total === 0 ? '这个来源里没有找到对话。' : '当前筛选下没有对话。试试关掉「只看未导入」或清空搜索。'}
              </Empty>
            ) : (
              <div className="max-h-[28rem] divide-y divide-white/[0.05] overflow-y-auto">
                {visible.map((item) => (
                  <ImportRow
                    key={item.session_id}
                    item={item}
                    checked={selected.has(item.session_id)}
                    onToggle={() => toggle(item.session_id)}
                  />
                ))}
              </div>
            )}
          </Card>

          <Card icon={IconSliders} title="导入选项">
            <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
              <Toggle checked={summarize} onChange={setSummarize} label="导入后切成记忆切片" />
              <label className="flex items-center gap-2 text-xs text-slate-400">
                每个对话只取最近
                <input
                  value={maxMessages}
                  inputMode="numeric"
                  placeholder="全部"
                  onChange={(event) => setMaxMessages(event.target.value)}
                  className="dm-field w-20 text-center tabular-nums"
                />
                条消息
              </label>
              <div className="ml-auto flex items-center gap-2">
                <Button icon={IconEye} disabled={busy || selected.size === 0} onClick={() => void run(true)}>
                  预览
                </Button>
                <Button
                  variant="primary"
                  icon={busy ? undefined : IconDownload}
                  disabled={busy || selected.size === 0}
                  onClick={() => void run(false)}
                >
                  {busy && <Spinner label="导入中" />}
                  导入 {selected.size} 个对话
                </Button>
              </div>
            </div>
            <p className="mt-3 text-2xs text-slate-500">
              导入是<strong className="font-medium text-slate-300">幂等</strong>的：同一个对话反复导入不会产生重复记忆，所以可以放心分批来。
            </p>
          </Card>
        </>
      )}

      {report && <ReportCard report={report} />}
    </div>
  )
}
