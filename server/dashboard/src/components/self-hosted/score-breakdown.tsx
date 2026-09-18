"use client";

import { Memory, ScoreDetails } from "@/types/api";
import {
  DECAY_NOTE,
  formatCount,
  formatDays,
  formatScore,
} from "@/utils/mechanism";

/**
 * 检索分量的逐条明细（`explain=true` 的 `score_details`）。
 *
 * 三条硬口径：
 * - **分母必须显示**（`max_possible_score`）：没有它用户拿不到复算口径。
 * - **键缺失即隐藏**：衰减分量与图分量只在对应能力开启时由后端发布，关闭态不得用
 *   0.0 占位（那会把「没参与打分」读成「贡献为 0」）。
 * - **图分量与图键、`graph_facts` 同屏**：`graph_boost = 0` 只在候选池里没有任何条目被
 *   图事实引用时出现，与「图键为空」在响应上同形，必须靠这两个读数区分。
 */
export function ScoreBreakdown({
  memory,
  graphKey,
}: {
  memory: Memory;
  graphKey?: string | null;
}) {
  const details: ScoreDetails | undefined = memory.score_details;
  if (!details) return null;

  const hasDecay = details.decay_weight !== undefined;
  const hasGraph = details.graph_boost !== undefined;

  const rows: { label: string; value: string; hint?: string }[] = [
    {
      label: "语义 semantic_score",
      value: formatScore(details.semantic_score),
    },
    { label: "关键词 bm25_score", value: formatScore(details.bm25_score) },
    { label: "实体 entity_boost", value: formatScore(details.entity_boost) },
  ];

  if (hasGraph) {
    rows.push({
      label: "图 graph_boost",
      value: formatScore(details.graph_boost),
      hint: `被图事实引用的事实条数 graph_facts = ${formatCount(
        details.graph_facts,
      )}；0 只表示候选池里没有条目被图引用`,
    });
  }

  rows.push(
    { label: "合计 raw_score", value: formatScore(details.raw_score) },
    {
      label: "分母 max_possible_score",
      value: formatScore(details.max_possible_score),
      hint: "四信号全开时为 3.0；只有候选池里确有图加分时才计入 0.5",
    },
    { label: "阈值 threshold", value: formatScore(details.threshold) },
    { label: "最终 final_score", value: formatScore(details.final_score) },
  );

  const decayRows: { label: string; value: string }[] = hasDecay
    ? [
        {
          label: "时间因子 decay_weight",
          value: formatScore(details.decay_weight),
        },
        { label: "保留率 retention", value: formatScore(details.retention) },
        {
          label: "记忆强度 memory_strength_days",
          value: formatDays(details.memory_strength_days),
        },
        { label: "经过 elapsed_days", value: formatDays(details.elapsed_days) },
        {
          label: "召回次数 access_count",
          value: formatCount(details.access_count),
        },
      ]
    : [];

  return (
    <div className="space-y-2">
      <p className="text-xs text-onSurface-default-tertiary">分值构成</p>
      <div className="divide-y divide-memBorder-primary rounded-md border border-memBorder-primary">
        {rows.map((row) => (
          <div
            key={row.label}
            className="flex items-start justify-between gap-3 px-3 py-1.5"
          >
            <div className="min-w-0">
              <span className="text-xs text-onSurface-default-secondary">
                {row.label}
              </span>
              {row.hint && (
                <p className="text-[10px] text-onSurface-default-tertiary">
                  {row.hint}
                </p>
              )}
            </div>
            <span className="shrink-0 font-mono text-xs">{row.value}</span>
          </div>
        ))}
      </div>

      {hasGraph && (
        <p className="text-[10px] text-onSurface-default-tertiary">
          本次检索所用的图键：
          <span className="font-mono">{graphKey ?? "--"}</span>
          。图键为空或图未命中都表现为 graph_boost = 0，需与上方 graph_status
          一起读。
        </p>
      )}

      {hasDecay ? (
        <div className="space-y-1">
          <div className="divide-y divide-memBorder-primary rounded-md border border-memBorder-primary">
            {decayRows.map((row) => (
              <div
                key={row.label}
                className="flex items-center justify-between gap-3 px-3 py-1.5"
              >
                <span className="text-xs text-onSurface-default-secondary">
                  {row.label}
                </span>
                <span className="shrink-0 font-mono text-xs">{row.value}</span>
              </div>
            ))}
          </div>
          <p className="text-[10px] text-onSurface-default-tertiary">
            {DECAY_NOTE}
          </p>
        </div>
      ) : (
        <p className="text-[10px] text-onSurface-default-tertiary">
          本次打分未包含时间因子（衰减关闭时后端不发布这些分量）。
        </p>
      )}
    </div>
  );
}
