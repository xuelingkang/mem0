import { Memory } from "@/types/api";

/**
 * 清单的归属者过滤条件（`user_id` / `agent_id`）。
 *
 * 空值表示该维度不约束——两个维度同时给出时取交集，与后端 `GET /memories` 的
 * `filters` 取交集口径一致。
 */
export interface OwnerFilter {
  user_id?: string;
  agent_id?: string;
}

/** 是否存在生效的过滤维度（只填空白字符不算）。 */
export const hasOwnerFilter = (filter: OwnerFilter): boolean =>
  Boolean(filter.user_id?.trim() || filter.agent_id?.trim());

/**
 * 单条记忆是否落在归属者过滤范围内：逐维度精确等值（两侧 trim），未约束的维度放行。
 *
 * 用等值而不是子串匹配：过滤的语义是「限定集合」，与检索的相关性排序是两件事；
 * 子串匹配会把 `ying` 也匹配到 `ying2` 这类另一个归属者上。
 */
export const matchesOwnerFilter = (
  memory: Memory,
  filter: OwnerFilter,
): boolean => {
  const userId = filter.user_id?.trim();
  if (userId && memory.user_id !== userId) return false;
  const agentId = filter.agent_id?.trim();
  if (agentId && memory.agent_id !== agentId) return false;
  return true;
};
