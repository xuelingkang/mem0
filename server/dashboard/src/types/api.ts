/** 图分支状态：一次检索一次汇总（`graph_status`）。 */
export type GraphStatus = "ok" | "skipped" | "timeout" | "error" | "disabled";

/**
 * `explain=true` 时的分值构成。
 *
 * 语义 / 关键词 / 实体三分量恒定出现；衰减分量与图分量只在对应能力开启时出现，
 * 因此都标为可选——键缺失即「该机制未参与本次打分」，而不是 0。
 */
export interface ScoreDetails {
  semantic_score: number;
  bm25_score: number;
  entity_boost: number;
  raw_score: number;
  /** 分母：四信号全开时为 3.0。没有它就复算不出最终分。 */
  max_possible_score: number;
  final_score: number;
  threshold: number;
  // 时间因子（衰减开启时出现）
  decay_weight?: number;
  retention?: number;
  memory_strength_days?: number;
  elapsed_days?: number;
  access_count?: number | null;
  // 图信号（图能力开启时出现，未命中为 0）
  graph_boost?: number;
  graph_facts?: number;
}

/**
 * 记忆 / 观察条目。机制字段（bi-temporal、衰减足迹、观察身份）都由 `GET /memories`
 * 与 `POST /search` 作为一等字段返回，仅在观察条目上取非空值。
 */
export interface Memory {
  id: string;
  memory: string;
  user_id?: string | null;
  agent_id?: string | null;
  run_id?: string | null;
  hash?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  expiration_date?: string | null;
  /** 生效时刻；存量记忆可能为 null。 */
  valid_at?: string | null;
  /** 失效时刻；非空即「已失效」。 */
  invalid_at?: string | null;
  /** 失效时取代它的那条记忆 id。 */
  superseded_by?: string | null;
  invalid_reason?: string | null;
  /** 上次被检索返回的时刻；从未召回时为 null。 */
  last_accessed?: string | null;
  /** 召回次数；从未召回时为 null。 */
  access_count?: number | null;
  /** `observation` 表示 Dream 合成的信念条目。 */
  memory_kind?: string | null;
  observation_key?: string | null;
  source_memory_ids?: string[] | null;
  evidence_count?: number | null;
  dream_run_id?: string | null;
  score?: number;
  score_details?: ScoreDetails;
  graph_status?: GraphStatus;
  metadata?: Record<string, unknown>;
}

/** `GET /observations` 的响应（与 `GET /memories` 同口径的游标分页）。 */
export interface ObservationListResponse {
  results: Memory[];
  next_cursor: string | null;
  total: number | null;
}

/** `GET /memories`（管理面列表）的响应。 */
export interface MemoryListResponse {
  results: Memory[];
  next_cursor?: string | null;
  has_more?: boolean;
  total?: number | null;
}

/** `GET /memories/{id}/observations`：反向追溯（事实 → 它派生的观察）。 */
export interface MemoryObservationsResponse {
  memory_id: string;
  total: number;
  results: Memory[];
}

/** `GET /memories/{id}/sources`：正向追溯（观察 → 它的源事实）。 */
export interface MemorySourcesResponse {
  observation_id: string;
  total: number;
  /** 源事实已不存在（被删除 / 被取代后清理）的条数，原样呈现。 */
  missing: number;
  results: Memory[];
}

/** `GET /graph/stats`：进程内计数 + 熔断状态。计数归当前进程实例。 */
export interface GraphStats {
  enabled: boolean;
  timeout_seconds: number | null;
  circuit_open: boolean;
  consecutive_failures: number;
  queue_size: number;
  graph_dispatched: number;
  graph_synced: number;
  graph_already_synced: number;
  graph_failed: number;
  graph_dropped: number;
}

/** 一个图键的规模；三者全为 0 即空图键。 */
export interface GraphKeyScale {
  group_id: string;
  episodes: number;
  entity_nodes: number;
  entity_edges: number;
}

/** `GET /graph/keys`：`degraded` 表示读数失败（≠ 规模为 0）。 */
export interface GraphKeysResponse {
  keys: GraphKeyScale[];
  degraded: boolean;
}

/** 一轮 Dream 整合的运行审计（`GET /dream/runs`）。 */
export interface DreamRun {
  id: string;
  mode: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  scopes: number;
  clusters: number;
  llm_calls: number;
  failed_clusters: number;
  observations_written: number;
  observations_superseded: number;
  prompt_tokens: number;
  completion_tokens: number;
  duration_seconds: number | null;
  report_path: string | null;
}

export interface DreamRunsResponse {
  total: number;
  limit: number;
  offset: number;
  results: DreamRun[];
}

/** dry-run 报告的候选簇（`POST /dream/preview`）。 */
export interface DreamPreviewCandidate {
  observation_key: string;
  members: string[];
  text: string;
  source_memory_ids: string[];
  evidence_count: number;
  counterexample: unknown[];
  decision: string;
  skip_reason: string | null;
}

export interface DreamPreviewTotals {
  facts: number;
  observations: number;
  clusters: number;
  llm_calls: number;
  failed_clusters: number;
  prompt_tokens: number;
  completion_tokens: number;
  clusters_skipped: number;
  clusters_deferred: number;
}

/** dry-run 报告：零写入，唯一产物是一份报告文件。 */
export interface DreamPreviewReport {
  run_id: string;
  mode: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number;
  totals: DreamPreviewTotals;
  candidates: DreamPreviewCandidate[];
  supersede_preview: unknown[];
  would_write: string[];
  observations_written: number;
  observations_superseded: number;
  errors: string[];
  report_path?: string;
}

export interface ApiKey {
  id: string;
  label: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
}

export interface ApiKeyCreateResponse {
  id: string;
  label: string;
  key: string;
  key_prefix: string;
  created_at: string;
}

export interface ApiRequestLog {
  id: string;
  created_at: string;
  method: string;
  path: string;
  status_code: number;
  latency_ms: number;
  auth_type: string;
}

export interface RequestStatsDay {
  date: string;
  total: number;
  errors: number;
  avg_latency_ms: number;
}

export interface RequestStatsStatus {
  status_code: number;
  count: number;
}

export interface RequestStatsPath {
  path: string;
  count: number;
  avg_latency_ms: number;
}

export interface RequestStats {
  total: number;
  success_rate: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  window_days: number;
  by_day: RequestStatsDay[];
  by_status: RequestStatsStatus[];
  top_paths: RequestStatsPath[];
}

export type EntityType = "user" | "agent" | "run";

export interface Entity {
  id: string;
  type: EntityType;
  total_memories: number;
  created_at: string | null;
  updated_at: string | null;
}
