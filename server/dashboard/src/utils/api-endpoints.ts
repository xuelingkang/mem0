export const AUTH_ENDPOINTS = {
  SETUP_STATUS: "/auth/setup-status",
  REGISTER: "/auth/register",
  LOGIN: "/auth/login",
  REFRESH: "/auth/refresh",
  ME: "/auth/me",
  CHANGE_PASSWORD: "/auth/change-password",
  ONBOARDING_COMPLETE: "/auth/onboarding-complete",
} as const;

export const MEMORY_ENDPOINTS = {
  BASE: "/memories",
  BY_ID: (memoryId: string) => `/memories/${memoryId}`,
  HISTORY: (memoryId: string) => `/memories/${memoryId}/history`,
  EXPORT: "/memories/export",
  SEARCH: "/search",
  CONFIGURE: "/configure",
  CONFIGURE_PROVIDERS: "/configure/providers",
  RESET: "/reset",
  GENERATE_INSTRUCTIONS: "/generate-instructions",
  /** 反向追溯：某条事实派生的观察（只读）。 */
  OBSERVATIONS_OF: (memoryId: string) => `/memories/${memoryId}/observations`,
  /** 正向追溯：某条观察的源事实（只读）。 */
  SOURCES_OF: (memoryId: string) => `/memories/${memoryId}/sources`,
} as const;

/** Dream 观察清单（新增只读端点，服务端按 `memory_kind` 索引过滤）。 */
export const OBSERVATION_ENDPOINTS = {
  BASE: "/observations",
} as const;

/** Dream 运行审计与 dry-run 预览。 */
export const DREAM_ENDPOINTS = {
  RUNS: "/dream/runs",
  RUN_BY_ID: (runId: string) => `/dream/runs/${runId}`,
  PREVIEW: "/dream/preview",
} as const;

/** 图能力观测（计数来自进程内派发器；图键规模经 mem0 侧代理图桥）。 */
export const GRAPH_ENDPOINTS = {
  STATS: "/graph/stats",
  KEYS: "/graph/keys",
} as const;

export const API_KEY_ENDPOINTS = {
  BASE: "/api-keys",
  BY_ID: (keyId: string) => `/api-keys/${keyId}`,
} as const;

export const REQUEST_ENDPOINTS = {
  BASE: "/requests",
  STATS: "/requests/stats",
} as const;

export const ENTITY_ENDPOINTS = {
  BASE: "/entities",
  BY_ID: (type: string, id: string) =>
    `/entities/${type}/${encodeURIComponent(id)}`,
} as const;
