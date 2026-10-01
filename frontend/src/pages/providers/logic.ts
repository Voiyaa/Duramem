import type { providerApi } from '../../api'
import type { ProviderField } from '../../types'

type SaveResult = Awaited<ReturnType<typeof providerApi.patch>>

/** 只提交改动过的字段。密钥留空即"不改"，所以空串不进提交体。 */
export function collectDirty(draft: Record<string, string>, fields: ProviderField[]) {
  const out: Record<string, unknown> = {}
  for (const [name, raw] of Object.entries(draft)) {
    const field = fields.find((item) => item.name === name)
    if (!field) continue
    if (field.type === 'secret' && raw.trim() === '') continue
    if (field.type === 'int') {
      const number = Number(raw)
      if (Number.isNaN(number)) continue
      out[name] = number
    } else {
      out[name] = raw
    }
  }
  return out
}

/** 测试走草稿：只带这一组里填过的字段；密钥留空就用已保存的那个。 */
export function testValues(draft: Record<string, string>, fields: ProviderField[]) {
  const values: Record<string, unknown> = {}
  for (const field of fields) {
    const raw = draft[field.name]
    if (raw === undefined) continue
    if (field.type === 'secret' && raw.trim() === '') continue
    values[field.name] = field.type === 'int' ? Number(raw) : raw
  }
  return values
}

/** 把保存结果翻译成界面上的话。stale 为 null 表示不动「别忘了重建」那条提醒。 */
export function describeSave(result: SaveResult): {
  error: string | null
  notice: string | null
  stale: string[] | null
} {
  const rejected = Object.entries(result.rejected)
  const error = rejected.length > 0 ? rejected.map(([key, reason]) => `${key}：${reason}`).join('；') : null
  let notice: string | null = null
  let stale: string[] | null = null
  if (result.identity_changed) {
    stale = result.vectors_marked_stale
    notice =
      `嵌入提供方已从 ${result.previous_provider} 改为 ${result.effective_embedding.provider}。` +
      (result.vectors_marked_stale.length > 0
        ? `这些库的向量必须重建后才能检索：${result.vectors_marked_stale.join('、')}`
        : '当前没有库持有向量，无需重建。')
  } else if (Object.keys(result.applied).length > 0) {
    stale = []
    notice = `已应用：${Object.keys(result.applied).join('、')}`
  }
  if (result.auto_cleared_offline) {
    notice = `${notice ?? ''}（填了 Key，已自动关闭"强制离线哈希嵌入"——否则 Key 不会生效）`.trim()
  }
  return { error, notice, stale }
}
