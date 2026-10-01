import type {
  ActiveDbInfo,
  Chunk,
  ColdStartInfo,
  DatabaseInfo,
  DebugResponse,
  McpLastCall,
  Health,
  ImportArchiveResult,
  ImportConversation,
  ImportDefault,
  ImportListing,
  ImportReport,
  ImportSourceInfo,
  ProviderPatchResult,
  ProviderTestResult,
  ProviderView,
  SearchResponse,
  Session,
  SessionLayer,
  SessionRefreshResponse,
  SettingsView,
  Stats,
  SummaryRun,
} from './types'

/** 把后端各种形状的错误体转成一句人话。

    不能直接 String(detail)：FastAPI 的参数校验错误（422）的 detail 是
    一个对象数组，String() 会得到 "[object Object]"——用户看到这个等于没看到，
    而且会把真实的集成问题（比如请求的 limit 超过了后端上限）藏起来。
    */
function describeError(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail

  if (typeof detail === 'string') return detail

  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (item && typeof item === 'object') {
        const entry = item as { msg?: string; loc?: unknown[]; type?: string }
        const where = Array.isArray(entry.loc)
          ? entry.loc.filter((seg) => seg !== 'body' && seg !== 'query').join('.')
          : ''
        const msg = entry.msg ?? entry.type ?? '校验失败'
        return where ? `${where}: ${msg}` : msg
      }
      return String(item)
    })
    if (parts.length > 0) {
      return `请求参数不合法（HTTP ${status}）— ${parts.join('；')}`
    }
  }

  if (detail && typeof detail === 'object') {
    return JSON.stringify(detail)
  }

  if (typeof body === 'string' && body.trim()) return body.slice(0, 300)

  return `请求失败（HTTP ${status}）`
}
export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'content-type': 'application/json' },
    ...init,
  })
  const text = await response.text()
  let body: unknown = null
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = text
    }
  }
  if (!response.ok) {
    throw new ApiError(response.status, describeError(body, response.status))
  }
  return body as T
}

const qs = (params: Record<string, string | number | boolean | undefined | null>) => {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const api = {
  health: () => request<Health>('/api/health'),

  // 库管理
  listDatabases: () => request<{ databases: DatabaseInfo[] }>('/api/databases'),
  createDatabase: (name: string) =>
    request<{ ok: boolean; name: string }>('/api/databases', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  renameDatabase: (name: string, newName: string, renameFile = false) =>
    request<{ ok: boolean; name: string; file_path: string }>(
      `/api/databases/${encodeURIComponent(name)}`,
      { method: 'PATCH', body: JSON.stringify({ new_name: newName, rename_file: renameFile }) },
    ),
  coldStart: (name: string) =>
    request<ColdStartInfo>(`/api/databases/${encodeURIComponent(name)}/cold-start`),
  setColdStart: (name: string, payload: { note?: string; enabled?: boolean }) =>
    request<ColdStartInfo>(`/api/databases/${encodeURIComponent(name)}/cold-start`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  /** 最近一次 MCP 工具调用的输入/输出（MCP 子进程落盘的快照）。 */
  lastMcpCall: () => request<McpLastCall>('/api/mcp/last-call'),
  deleteDatabase: (name: string, purgeFile = false) =>
    request<{
      ok: boolean
      purged: boolean
      /** 只有 purge 时才可能为 true；false 表示文件还在（被别的进程占着） */
      file_removed: boolean
      leftover?: string[]
      warnings?: string[]
    }>(`/api/databases/${encodeURIComponent(name)}${qs({ purge_file: purgeFile })}`, {
      method: 'DELETE',
    }),
  stats: (db: string) => request<Stats>(`/api/databases/${encodeURIComponent(db)}/stats`),

  /** 当前记忆库（前端选定、模型跟随）。与"界面正在浏览哪个库"是两件事。 */
  activeDb: () => request<ActiveDbInfo>('/api/active-db'),
  setActiveDb: (name: string) =>
    request<{ ok: boolean; db: string; updated_at: string }>('/api/active-db', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  snapshot: (db: string) =>
    request<{ ok: boolean; path: string; size_bytes: number }>(
      `/api/snapshot${qs({ db })}`,
      { method: 'POST' },
    ),
  /** 导出可读 JSON 归档。走浏览器下载（后端带 Content-Disposition），不进内存。 */
  exportArchive: (db: string) => {
    const a = document.createElement('a')
    a.href = `/api/export${qs({ db })}`
    a.download = ''
    document.body.appendChild(a)
    a.click()
    a.remove()
  },
  /** 上传导出归档，还原成一个新库。请求体是归档文件本身，参数走 query。 */
  importArchive: (
    file: File,
    opts: { db?: string; dryRun?: boolean; reindex?: boolean } = {},
  ) =>
    request<ImportArchiveResult>(
      `/api/import-archive/upload${qs({
        db: opts.db,
        dry_run: opts.dryRun,
        reindex: opts.reindex,
      })}`,
      { method: 'POST', body: file },
    ),

  // 切片
  listChunks: (params: {
    db: string
    query?: string
    include_deleted?: boolean
    include_superseded?: boolean
    limit?: number
  }) =>
    request<{ chunks: Chunk[]; total: number }>(
      `/api/chunks${qs({
        db: params.db,
        query: params.query,
        include_deleted: params.include_deleted,
        include_superseded: params.include_superseded,
        limit: params.limit ?? 200,
      })}`,
    ),
  getChunk: (uid: string, db: string) =>
    request<Chunk>(`/api/chunks/${encodeURIComponent(uid)}${qs({ db })}`),
  patchChunk: (uid: string, db: string, patch: Record<string, unknown>) =>
    request<{ ok: boolean; applied: string[]; warnings?: string[] }>(
      `/api/chunks/${encodeURIComponent(uid)}${qs({ db })}`,
      { method: 'PATCH', body: JSON.stringify(patch) },
    ),
  /** 默认软删除（可恢复）。purge=true 才是真删除，不可恢复。 */
  deleteChunk: (uid: string, db: string, purge = false) =>
    request<{ ok: boolean; message?: string; uid: string; title?: string; unlinked_superseded?: number; note?: string }>(
      `/api/chunks/${encodeURIComponent(uid)}${qs({ db, purge })}`,
      { method: 'DELETE' },
    ),
  restoreChunk: (uid: string, db: string) =>
    request<{ ok: boolean }>(`/api/chunks/${encodeURIComponent(uid)}/restore${qs({ db })}`, {
      method: 'POST',
    }),

  // 会话层（容器层）：按会话看记忆，而不是按切片
  sessionLayers: (db: string) =>
    request<{ sessions: SessionLayer[] }>(`/api/session-layers${qs({ db })}`),
  getSessionLayer: (sessionId: string, db: string, windowId?: string) =>
    request<SessionLayer>(
      `/api/session-layers/${encodeURIComponent(sessionId)}${qs({ db, window_id: windowId })}`,
    ),
  /** 给 session_id 时只刷新那一个；否则按 freshness 策略批量刷新。 */
  refreshSessionLayers: (
    db: string,
    body: { session_id?: string; window_id?: string; force?: boolean; limit?: number } = {},
  ) =>
    request<SessionRefreshResponse>(`/api/session-layers/refresh${qs({ db })}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // 检索
  search: (query: string, db: string, topK?: number) =>
    request<SearchResponse>('/api/search', {
      method: 'POST',
      body: JSON.stringify({ query, db, top_k: topK }),
    }),
  debugSearch: (query: string, db: string, topK?: number) =>
    request<DebugResponse>('/api/debug/search', {
      method: 'POST',
      body: JSON.stringify({ query, db, top_k: topK }),
    }),

  // 会话与总结
  sessions: (db: string) => request<{ sessions: Session[] }>(`/api/sessions${qs({ db })}`),
  summaryRuns: (db: string, limit = 50) =>
    request<{ runs: SummaryRun[] }>(`/api/summary-runs${qs({ db, limit })}`),
  summarize: (windowId: string, sessionId: string, db: string) =>
    request<{
      ok: boolean
      added: number
      duplicates: number
      rejected: number
      warnings?: string[]
      msg_range?: [number, number]
    }>('/api/summarize', {
      method: 'POST',
      body: JSON.stringify({ window_id: windowId, session_id: sessionId, db }),
    }),

  // 运维与设置
  reindex: (db: string) =>
    request<{ db: string; embedded: number }>(`/api/reindex${qs({ db })}`, { method: 'POST' }),
  settings: () => request<SettingsView>('/api/settings'),
  patchSettings: (patch: Record<string, unknown>) =>
    request<{ ok: boolean; applied: Record<string, unknown>; rejected: Record<string, string> }>(
      '/api/settings',
      { method: 'PATCH', body: JSON.stringify(patch) },
    ),
  resetSettings: (keys?: string[]) =>
    request<{ reset: string[] }>('/api/settings/reset', {
      method: 'POST',
      body: JSON.stringify({ keys: keys ?? null }),
    }),
}

/* ==================================================================== 模型配置 */

export const providerApi = {
  view: () => request<ProviderView>('/api/provider-config'),

  /** 改模型配置并热生效。密钥字段只在写入时出现，读回永远是掩码。 */
  patch: (values: Record<string, unknown>) =>
    request<ProviderPatchResult>('/api/provider-config', {
      method: 'PATCH',
      body: JSON.stringify(values),
    }),

  reset: (keys?: string[]) =>
    request<{ reset: string[]; vectors_marked_stale: string[] }>('/api/provider-config/reset', {
      method: 'POST',
      body: JSON.stringify({ keys: keys ?? null }),
    }),

  /** 拿草稿试连通性，不保存。 */
  test: (group: string, values: Record<string, unknown>) =>
    request<ProviderTestResult>('/api/provider-config/test', {
      method: 'POST',
      body: JSON.stringify({ group, values }),
    }),

  rebuildVectors: (db: string) =>
    request<{ db: string; embedded: number; embedding_model: string; embedding_dim: number }>(
      `/api/rebuild-vectors${qs({ db })}`,
      { method: 'POST' },
    ),
}

/** 解析后端存的 JSON 数组字段。 */
export function parseList(raw: string | null | undefined): string[] {
  if (!raw) return []
  try {
    const value = JSON.parse(raw)
    return Array.isArray(value) ? value.map(String) : []
  } catch {
    return []
  }
}

export function formatBytes(bytes: number | undefined): string {
  if (!bytes) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function shortDate(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return `${date.getMonth() + 1}/${date.getDate()}`
}
/* ==================================================================== 历史导入 */

export const importApi = {
  sources: () => request<{ sources: ImportSourceInfo[]; defaults: ImportDefault[] }>('/api/import/sources'),

  list: (payload: {
    source?: string
    path?: string
    db?: string
    include_subagents?: boolean
    since_days?: number
    refresh?: boolean
  }) =>
    request<ImportListing>('/api/import/list', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  run: (payload: {
    source?: string
    path?: string
    db?: string
    sessions: string[]
    summarize?: boolean
    dry_run?: boolean
    max_messages?: number
    include_subagents?: boolean
  }) =>
    request<ImportReport>('/api/import', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
}
