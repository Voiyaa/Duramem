/** 与后端 REST 形状对应的类型。字段名刻意与 /api 返回一致，避免中间层翻译。 */

export interface Health {
  ok: boolean
  version: string
  databases: string[]
  embedding_model: string
  /** 实际生效的提供方。没填 Key 时是 offline-hashing-1024，而不是配置里写的模型名。 */
  embedding_provider?: string
  offline_embedding: boolean
  rerank_enabled: boolean
}

/** 每库的冷启动注入配置（存在库文件 db_meta 里，随文件走）。 */
export interface ColdStartInfo {
  db: string
  note: string
  enabled: boolean
}

/** 最近一次 MCP 工具调用的快照（MCP 子进程写、REST 读）。 */
export interface McpLastCall {
  exists: boolean
  tool?: string
  db?: string | null
  arguments?: Record<string, unknown>
  output?: unknown
  cold_start_injected?: boolean
  duration_ms?: number
  ts?: string
  pid?: number
  error?: string
}

export interface DatabaseInfo {
  name: string
  file_path: string
  db_uuid: string
  created_at: string
  chunks_alive?: number
  messages?: number
  vectors?: number
  chunks_missing_vectors?: number
  size_bytes?: number
  wal_bytes?: number
  embedding_model?: string
  embedding_dim?: number
  /** 库内向量实际由谁生成。空 = 还没写过向量（或旧库未记录）。 */
  vector_source?: string
  vectors_stale?: boolean
  compatible: boolean
  /** 是不是"当前记忆库"（前端选定、模型跟随的那个）。 */
  active?: boolean
  /** 该库自己的冷启动注入配置。库打不开（模型不一致等）时缺省。 */
  cold_start?: ColdStartInfo
  error?: string
}

/** 当前记忆库指针：前端选定、MCP 下一轮检索跟随。 */
export interface ActiveDbInfo {
  db: string | null
  source: string
  exists: boolean
  updated_at?: string | null
  updated_by?: string | null
  warning?: string | null
  /** 指针没选定或失效时，实际会退到哪个库 */
  fallback?: string | null
  error?: string
}

export interface Chunk {
  id: number
  chunk_uid: string
  title: string
  title_suggested: string | null
  summary_text: string
  /** 原文。三层里最细的一层，按需回查。 */
  original_text: string
  keywords: string
  tags: string
  source_db: string | null
  source_window: string | null
  source_session: string | null
  msg_id_start: number | null
  msg_id_end: number | null
  /** 相对 original_text 的字符偏移 */
  original_char_start: number | null
  original_char_end: number | null
  content_hash: string
  hit_count: number
  weight: number
  created_at: string
  updated_at: string
  deleted_at: string | null
  superseded_by: string | null
  summary_tokens: number
  summary_truncated: number
  db?: string
  neighbours?: { uid: string; title: string; summary_text: string }[]
}

export interface SearchHit {
  uid: string
  title: string
  summary: string
  score: number
  hit_count: number
  has_original: boolean
  /** 该切片所属会话。据此可取会话概览。 */
  session_id?: string
  db?: string
  msg_range?: [number, number]
  _suggestion?: string
}

/** 会话命中。只带 L0 摘要——概览正文（L1）按需用 getSessionLayer 取。 */
export interface SessionHit {
  session_id: string
  abstract: string
  slice_count: number
  score: number
  db?: string
  _suggestion?: string
}

/** 会话层（容器层）：一段会话的简介 + 概览。 */
export interface SessionLayer {
  id: number
  window_id: string
  session_id: string
  abstract_text: string
  overview_text: string
  abstract_tokens: number
  overview_tokens: number
  model_used: string | null
  overview_digest: string | null
  /** 累计"子切片变了但概览还没跟上"的次数。> 0 表示概览可能落后。 */
  pending_changes: number
  /** 生成时的切片总数（概览里"覆盖度"一段的依据） */
  coverage_total: number
  coverage_sampled: number
  created_at: string
  updated_at: string
}

export interface SessionRefreshEntry {
  window_id: string
  session_id: string
  slice_count: number
  decision: string
  reason: string
  ok: boolean | null
  warnings: string[]
}

export interface SessionRefreshResponse {
  ok: boolean
  db: string
  candidates: number
  refreshed: number
  results: SessionRefreshEntry[]
}

export interface SearchResponse {
  results: SearchHit[]
  /** 会话命中单独一段，不与切片混排（两者分数量纲不该可比） */
  sessions?: SessionHit[]
  session_count?: number
  retrieval_mode: string
  count: number
  warnings?: string[]
  _suggestion?: string
  debug?: unknown
}

export type Arm = 'both' | 'vector_only' | 'lexical_only' | 'none'

export interface ArmItem {
  chunk_id: number
  uid: string | null
  title: string | null
  summary: string | null
  arm: Arm
  vec_rank: number | null
  lex_rank: number | null
}

export interface VectorItem extends ArmItem {
  rank: number
  distance: number
}

export interface LexicalItem {
  rank: number
  chunk_id: number
  uid: string
  title: string
  summary: string
  bm25: number
}

export interface DebugResponse {
  query: string
  db: string
  retrieval_mode: string
  warnings: string[]
  params: Record<string, number>
  arms: { vector: ArmItem[]; lexical: ArmItem[] }
  vector: VectorItem[]
  lexical: LexicalItem[]
  fusion_pool: ArmItem[]
  reranked: boolean
  /* 会话路（容器层）。与切片路并列，用来看"冷启动为什么命中了这个会话"。 */
  session_vector?: number[]
  session_lexical?: number[]
  session_fusion_pool?: number[]
  sessions?: SessionHit[]
  /* 图扩散（§16）。expand 轨迹：种子、触发原因与发射的候选。 */
  expand?: {
    triggered: boolean
    reason?: string | null
    error?: string
    note?: string
    seeds?: { uid: string; score: number }[]
    emitted?: {
      uid: string
      chunk_id: number
      ppr: number
      score: number
      hops: number
      relation: string
      via_seed: string | null
      path: number[]
    }[]
  }
  expanded?: (DebugResponse['final'][number] & {
    origin?: string
    expand_score?: number
    expand_via?: string | null
    expand_relation?: string | null
    expand_hops?: number | null
  })[]
  final: {
    uid: string
    title: string
    summary: string
    rrf_score: number
    float_score: number
    vec_rank: number | null
    vec_distance: number | null
    lex_rank: number | null
    lex_score: number | null
    rerank_rank: number | null
    rerank_score: number | null
    weight: number
    hit_count: number
    arm: Arm
  }[]
}

export interface Session {
  window_id: string
  session_id: string
  messages: number
  last_msg_id: number
}

export interface SummaryRun {
  id: number
  window_id: string | null
  session_id: string | null
  msg_id_start: number | null
  msg_id_end: number | null
  chunks_added: number
  duplicates: number
  rejected: number
  warnings: string
  model_used: string | null
  duration_ms: number | null
  created_at: string
}

export interface Stats {
  db: string
  chunks_total: number
  chunks_alive: number
  chunks_deleted: number
  chunks_superseded: number
  chunks_missing_vectors: number
  summary_truncated: number
  messages: number
  vectors: number
  size_bytes: number
  wal_bytes: number
  index: { backend: string; dim: number; vectors: number }
  rerank_enabled: boolean
  rerank_model: string
  embedding_provider: string
  cursors: { window_id: string; session_id: string; last_summarized_msg_id: number; updated_at: string }[]
  /** 精简投影：与模型 dm_stats 同一份事实，状态卡渲染的就是它。 */
  brief?: StatsBrief
}

/**
 * 统计的精简投影——前端状态卡与模型 dm_stats 看到的**同一份事实**
 * （service.stats_brief 一次性投影，两个消费者共用）。
 * 条件字段（vectors_stale / chunks_missing_vectors / summary_truncated_ratio /
 * warnings）出现即值得注意，不出现不占地方。
 */
export interface StatsBrief {
  db: string
  active_db: string | null
  chunks_alive: number
  vectors: number
  messages: number
  sessions_summarized: number
  latest_summary_at?: string
  vectors_stale?: true
  chunks_missing_vectors?: number
  summary_truncated_ratio?: number
  warnings?: string[]
}

export interface SettingField {
  name: string
  type: 'int' | 'float' | 'bool' | 'string'
  label: string
  group: string
  help: string
  value: string | number | boolean
  overridden: boolean
}

export interface SettingsView {
  tunable: SettingField[]
  readonly: Record<string, unknown>
  needs_restart: string[]
  overrides: Record<string, unknown>
  overrides_file: string
  resolved: {
    embedding_model: string
    embedding_dim: number
    offline_embedding: boolean
    rerank_model: string
    summary_model: string
    gateway_enabled: boolean
    data_dir: string
  }
}
/* ==================================================================== 模型配置 */

export interface ProviderField {
  name: string
  group: 'embedding' | 'rerank' | 'summary'
  label: string
  type: 'text' | 'secret' | 'int' | 'bool'
  help: string
  placeholder: string
  /** 密钥字段永远为空——只能看到 masked */
  value: string
  masked: string
  has_value: boolean | null
  overridden: boolean
}

export interface ProviderPreset {
  id: string
  label: string
  vendor: string
  values: Record<string, string | number | boolean>
  note: string
  needs_key: boolean
}

export interface ProviderGroup {
  id: 'embedding' | 'rerank' | 'summary'
  label: string
  help: string
}

export interface VectorState {
  name: string
  file_path: string
  vector_source: string
  chunks: number
  vectors_stale: boolean
  needs_rebuild: boolean
  /** false = 有向量但没记来源（本次改版前建的库），来源已无法还原 */
  source_known: boolean
  error: string
}

export interface ProviderView {
  fields: ProviderField[]
  overrides: Record<string, unknown>
  overrides_file: string
  groups: ProviderGroup[]
  presets: Record<string, ProviderPreset[]>
  effective_embedding: {
    provider: string
    model: string
    dim: number
    offline: boolean
    batch_size: number
    base_url: string
    send_dimensions: boolean
    has_key: boolean
    key_masked: string
  }
  rerank: {
    enabled: boolean
    model: string
    base_url: string
    has_key: boolean
    key_masked: string
    effective: string
  }
  summary: {
    model: string
    base_url: string
    has_key: boolean
    key_masked: string
    effective: string
  }
  databases: VectorState[]
}

export interface ProviderTestResult {
  ok: boolean
  group: string
  provider?: string
  offline?: boolean
  ms?: number
  note?: string
  error?: string
  detected_dim?: number
  configured_dim?: number
  batch_size?: number
  reply?: string
  scores?: number[]
  ranking_sane?: boolean
}

export interface ProviderPatchResult {
  ok: boolean
  applied: Record<string, unknown>
  rejected: Record<string, string>
  auto_cleared_offline?: boolean
  vectors_marked_stale: string[]
  identity_changed: boolean
  previous_provider?: string
  effective_embedding: ProviderView['effective_embedding']
}

/* ==================================================================== 历史导入 */

export interface ImportSourceInfo {
  name: string
  description: string
}

export interface ImportDefault {
  source: string
  path: string
  exists: boolean
  label: string
  hint: string
}

export interface ImportConversation {
  origin_id: string
  title: string
  window_id: string
  session_id: string
  messages: number
  created_at: string | null
  is_subagent: boolean
  first_line: string
  already_imported: boolean
}

export interface ImportListing {
  source: string
  path: string
  total: number
  conversations: ImportConversation[]
}

export interface ImportReport {
  ok: boolean
  source: string
  dry_run: boolean
  conversations_found: number
  conversations_imported: number
  messages_imported: number
  skipped_short: number
  skipped_empty: number
  summarized?: number
  chunks_added?: number
  errors?: string[]
  preview?: { origin_id: string; title: string; messages: number; created_at: string | null }[]
}

/** 归档导入结果（POST /api/import-archive*）。dry_run 时只有计数，不写库。 */
export interface ImportArchiveResult {
  ok: boolean
  db: string
  format_version: number
  dry_run?: boolean
  chunks: number
  messages?: number
  layers: number
  cursors: number
  runs: number
  vectors?: number
  session_vectors?: number
  warnings: string[]
}
