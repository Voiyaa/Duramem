import { Button, Card, Empty, ErrorBox, NoticeBox, Stat } from '../components/ui'
import {
  IconAlert,
  IconCheck,
  IconCpu,
  IconDatabase,
  IconHash,
  IconRefresh,
  IconSparkles,
  IconTrending,
  IconUndo,
} from '../components/icons'
import GroupCard from './providers/GroupCard'
import { VectorSourcesCard } from './providers/parts'
import { useProviders, type GroupId } from './providers/useProviders'

/**
 * 模型配置页。
 *
 * 三条设计取向，都是被具体问题逼出来的：
 *
 * 1. **密钥只写不读。** 输入框永远从空开始，保存后只回显掩码。留空保存
 *    不会把已存的 Key 抹掉——否则用户改个模型名就会顺手删掉密钥。
 * 2. **先测试再保存。** 测试走的是草稿配置：填完点测试通过，保存后才是同一个
 *    结果。维度填错会在这里被拦下来，而不是等到写库时才报错。
 * 3. **换嵌入模型 = 数据作废。** 所以要显式告诉用户"哪些库要重建"，并给出
 *    一键重建。含糊其辞的话，用户会带着混合向量继续用，检索结果无法解释。
 */
export default function Providers({ onChanged }: { onChanged?: () => void }) {
  const p = useProviders(onChanged)
  const { view, draft, error, notice, busy, testing, results, rebuilding, staleAfterSave, byGroup, dirtyCount } = p

  if (error && !view) return <ErrorBox>{error}</ErrorBox>
  if (!view) return <Empty icon={IconCpu}>正在读取模型配置…</Empty>

  const needRebuild = view.databases.filter((item) => item.needs_rebuild)
  const embedding = view.effective_embedding

  return (
    <div className="space-y-4">
      {error && <ErrorBox onClose={() => p.setError(null)}>{error}</ErrorBox>}
      {notice && <NoticeBox onClose={() => p.setNotice(null)}>{notice}</NoticeBox>}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat
          icon={IconSparkles}
          label="生效的嵌入"
          value={embedding.provider}
          hint={embedding.offline ? '离线哈希向量：只有词项重叠，没有语义泛化' : `配置的模型名是 ${embedding.model}`}
        />
        <Stat icon={IconHash} label="向量维度" value={String(embedding.dim)} />
        <Stat
          icon={IconTrending}
          label="重排"
          value={view.rerank.effective}
          hint={view.rerank.enabled ? '每次检索多一次 API 往返' : '默认关闭'}
        />
        <Stat
          icon={IconDatabase}
          label="需重建的库"
          value={needRebuild.length}
          hint={needRebuild.length > 0 ? '不重建就无法检索' : '向量与当前配置一致'}
        />
      </div>

      {embedding.offline && (
        <NoticeBox tone="warn">
          当前用的是<strong>离线哈希嵌入</strong>——没有 API Key 时服务会自动降级到它，让整条链路在没有网络的环境里也能跑通。
          它只在"共享词项多"时相似度高，<strong>没有语义泛化</strong>：问「端口被占用」能命中含"端口"的切片，
          但问「服务起不来」命中不了。在下面选一个预设、填入 API Key，测试通过后保存即可。
        </NoticeBox>
      )}

      {needRebuild.length > 0 && (
        <Card
          icon={IconAlert}
          title="这些库的向量需要重建"
          subtitle="换了嵌入提供方：库里的旧向量与新向量不在同一个空间里，混着查只会得到噪声"
        >
          <div className="dm-panel divide-y divide-white/[0.05]">
            {needRebuild.map((item) => (
              <div key={item.name} className="flex items-center gap-3 px-3.5 py-2.5">
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-medium text-slate-200">{item.name}</div>
                  <div className="text-2xs text-slate-500">
                    现有向量由 <span className="mono">{item.vector_source || '未知'}</span> 生成 · {item.chunks} 条切片
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="primary"
                  icon={IconRefresh}
                  disabled={rebuilding !== null}
                  onClick={() => void p.rebuild(item.name)}
                >
                  {rebuilding === item.name ? '重建中…' : '重建向量'}
                </Button>
              </div>
            ))}
          </div>
          <p className="mt-3 text-2xs leading-relaxed text-slate-500">
            重建会调用嵌入接口逐条重算，切片多时要等一会儿。期间该库的检索会被拒绝——这是故意的：宁可拒绝，也不返回无法解释的结果。
          </p>
        </Card>
      )}

      <div
        className={`sticky top-3 z-20 flex flex-wrap items-center gap-2 rounded-xl border px-3 py-2 backdrop-blur-xl ${
          dirtyCount > 0 ? 'border-sky-400/30 bg-sky-950/70' : 'border-white/[0.06] bg-ink-950/70'
        }`}
      >
        <Button variant="primary" icon={IconCheck} disabled={busy || dirtyCount === 0} onClick={() => void p.save()}>
          保存{dirtyCount > 0 ? `（${dirtyCount} 项）` : ''}
        </Button>
        <Button variant="ghost" icon={IconUndo} disabled={busy || Object.keys(draft).length === 0} onClick={p.discard}>
          放弃改动
        </Button>
        {Object.keys(view.overrides).length > 0 && (
          <Button variant="ghost" disabled={busy} onClick={() => void p.resetAll()}>
            全部恢复默认
          </Button>
        )}
        <span className="ml-auto min-w-0 truncate text-2xs text-slate-500">
          {dirtyCount > 0 && <span className="text-sky-200">有 {dirtyCount} 项未保存 · </span>}
          配置写在 <span className="mono">{view.overrides_file}</span>，改完立即生效，不用重启
        </span>
      </div>

      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
        {view.groups.map((group) => (
          <GroupCard
            key={group.id}
            group={group}
            view={view}
            fields={byGroup[group.id as GroupId] ?? []}
            draft={draft}
            busy={busy}
            testing={testing}
            result={results[group.id as GroupId]}
            onTest={() => void p.runTest(group.id as GroupId)}
            onPreset={p.applyPreset}
            onChange={p.set}
          />
        ))}
        <VectorSourcesCard databases={view.databases} />
      </div>

      {staleAfterSave.length > 0 && <NoticeBox tone="warn">别忘了重建：{staleAfterSave.join('、')}</NoticeBox>}
    </div>
  )
}
