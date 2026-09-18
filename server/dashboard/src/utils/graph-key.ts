/**
 * 图键（FalkorDB graph key）的前端派生。
 *
 * 与后端 `mem0/memory/graph_sync.py` 的 `derive_group_id` 同规则：作用域优先级
 * `user_id` → `agent_id` → `run_id`，取第一个非空值，非法字符归一为下划线，前缀
 * `mem0_`，最长 96 字符。页面展示这个派生值，只是为了把「本次检索用的是哪个图」与
 * `graph_facts` 同屏摆出来：`graph_boost = 0` 只有在候选池里没有任何条目被图事实引用
 * 时才出现，它与「图键为空 / 图没答上来」在响应上同形，必须靠图键 + `graph_status`
 * 才能区分。
 */

const GROUP_ID_PREFIX = "mem0";
const GROUP_ID_MAX_LENGTH = 96;
const GROUP_ID_INVALID = /[^0-9A-Za-z_-]/g;
const SCOPE_KEYS = ["user_id", "agent_id", "run_id"] as const;

/** 把作用域值归一为图键片段（与后端 `sanitize_group_id` 同规则）。 */
export function sanitizeGroupId(value: unknown): string {
  const text = String(value ?? "")
    .trim()
    .replace(GROUP_ID_INVALID, "_");
  return text.length > GROUP_ID_MAX_LENGTH
    ? text.slice(0, GROUP_ID_MAX_LENGTH)
    : text;
}

/** 由检索作用域派生图键；派生不出作用域时返回 null（该次检索不会发起图调用）。 */
export function deriveGraphKey(
  filters?: Record<string, unknown> | null,
): string | null {
  if (!filters) return null;
  for (const key of SCOPE_KEYS) {
    const value = filters[key];
    if (value) return `${GROUP_ID_PREFIX}_${sanitizeGroupId(value)}`;
  }
  return null;
}
