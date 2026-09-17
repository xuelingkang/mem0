export interface Memory {
  id: string;
  memory: string;
  user_id?: string;
  agent_id?: string;
  created_at?: string;
  updated_at?: string;
  score?: number;
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
