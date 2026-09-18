import { GraphStatus, Memory, ScoreDetails } from "@/types/api";

/**
 * 四项记忆机制在 UI 上的取值 → 文案映射与格式化口径。
 *
 * 这里的每个函数都只做「响应值到显示值」的直译，不做二次推断：机制字段缺失时一律
 * 显示 `--`（而不是 0 或「无」这类会被读成结论的值）。
 */

/** 生效/失效判定：只由 `invalid_at` 决定（存量记忆可能没有 `valid_at`）。 */
export const isInvalidated = (memory: Memory): boolean =>
  memory.invalid_at != null;

export const isObservation = (memory: Memory): boolean =>
  memory.memory_kind === "observation";

/** 图分支状态 → 文案。五种取值的文案两两不同，且都不把 0 加分表述为「图没命中」。 */
export const GRAPH_STATUS_TEXT: Record<GraphStatus, string> = {
  ok: "图分支已返回（命中数为 0 时也不代表图没答上来）",
  skipped: "图分支未发起调用（本次无作用域键或候选池为空）",
  timeout: "图分支超时，本次按无图信号处理",
  error: "图分支失败（图桥不可达或返回错误），本次按无图信号处理",
  disabled: "图能力已关闭（未发起任何图调用）",
};

export const GRAPH_STATUS_TONE: Record<
  GraphStatus,
  "positive" | "muted" | "warn" | "danger"
> = {
  ok: "positive",
  skipped: "muted",
  timeout: "warn",
  error: "danger",
  disabled: "muted",
};

const TONE_CLASS = {
  positive:
    "border-memGreen-200 bg-memGreen-50 text-onSurface-positive-primary",
  muted:
    "border-memBorder-primary bg-surface-default-tertiary text-onSurface-default-tertiary",
  warn: "border-memGold-200 bg-memGold-50 text-memGold-800",
  danger: "border-memRed-200 bg-memRed-50 text-onSurface-danger-primary",
} as const;

/** 状态条统一排版：状态徽章与图分支状态条共用同一套视觉。 */
export const STATUS_BADGE_BASE =
  "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium";

export const graphStatusClass = (status: GraphStatus): string =>
  `${STATUS_BADGE_BASE} ${TONE_CLASS[GRAPH_STATUS_TONE[status]]}`;

/** 数值格式化：分值 3 位（沿用既有 `toFixed(3)`），计数千分位，天数 1 位并标单位。 */
export const formatScore = (value: number | null | undefined): string =>
  typeof value === "number" ? value.toFixed(3) : "--";

export const formatCount = (value: number | null | undefined): string =>
  typeof value === "number" ? value.toLocaleString() : "--";

export const formatDays = (value: number | null | undefined): string =>
  typeof value === "number" ? `${value.toFixed(1)} 天` : "--";

export const formatTimestamp = (value: string | null | undefined): string =>
  value ? new Date(value).toLocaleString() : "--";

/** 日期（`YYYY-MM-DD`）按原样显示：它是 payload 里的日期口径，不额外加时刻。 */
export const formatDate = (value: string | null | undefined): string =>
  value ? String(value) : "--";

/** 分值构成中恒定出现的三分量 + 合计，用于逐行渲染分量表。 */
export const SCORE_BASE_ROWS: {
  key: keyof ScoreDetails;
  label: string;
  scale: number;
}[] = [
  { key: "semantic_score", label: "语义", scale: 3 },
  { key: "bm25_score", label: "关键词", scale: 3 },
  { key: "entity_boost", label: "实体", scale: 3 },
  { key: "graph_boost", label: "图", scale: 3 },
];

/**
 * 图键口径说明（与后端 `derive_group_id` 同规则）。
 *
 * 之所以必须把它与 `graph_facts` 一起摆出来：`graph_boost = 0` 只有当候选池里没有任何
 * 条目被图事实引用时才出现，它与「图键为空」在响应上同形，两者都表现为
 * `graph_boost = 0` 且 `graph_status = ok`。
 */
export const GRAPH_KEY_NOTE =
  "图键由作用域派生（user_id 优先 ⇒ mem0_<user_id>）；同一 user_id 的不同 agent_id 共用一个图键。";

/** 时间因子口径说明（与 `mem0/utils/scoring.py` 一致：只下调，上界 1）。 */
export const DECAY_NOTE = "时间因子 = 0.90 + 0.10 × 保留率，只下调、不放大。";

/** 进程内计数口径说明：换进程即归零。 */
export const PROCESS_COUNTER_NOTE = "自本进程启动以来";
