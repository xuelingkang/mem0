"use client";

import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { GraphStatus, Memory } from "@/types/api";
import {
  GRAPH_STATUS_TEXT,
  GRAPH_KEY_NOTE,
  graphStatusClass,
  isInvalidated,
} from "@/utils/mechanism";

/**
 * 机制状态徽章与图分支状态条。
 *
 * 取值一律直译自响应字段：有效/失效只看 `invalid_at`，观察只看 `memory_kind`。
 */

/** 有效 / 已失效（+ 被取代）。判定只由 `invalid_at` 决定。 */
export function ValidityBadge({ memory }: { memory: Memory }) {
  if (!isInvalidated(memory)) {
    return (
      <span className="text-xs text-onSurface-default-secondary">有效</span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1">
      <span className="text-xs font-medium text-onSurface-default-tertiary">
        已失效
      </span>
      {memory.superseded_by && (
        <span className="text-xs text-onSurface-danger-primary">被取代</span>
      )}
    </span>
  );
}

/** 观察标记：Dream 合成的信念条目，不是用户事实。 */
export function ObservationBadge() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          variant="outline"
          className="border-memPurple-200 bg-memPurple-50 text-memPurple-800 text-[10px]"
        >
          observation
        </Badge>
      </TooltipTrigger>
      <TooltipContent>
        由 Dream 后台整合合成的信念条目（memory_kind = observation）
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * 检索模式的图分支状态条：一次检索一条汇总。
 *
 * 为什么不做逐行徽章：实测同一响应内所有条目的 `graph_status` 同值，逐行重复同一状态
 * 只是噪声；它回答的问题（这次图分支答没答上来）本来就是整次检索的问题。
 */
export function GraphStatusBar({
  status,
  budgetSeconds,
  graphKey,
}: {
  status: GraphStatus;
  budgetSeconds?: number | null;
  graphKey?: string | null;
}) {
  const text =
    status === "timeout" && typeof budgetSeconds === "number"
      ? `${GRAPH_STATUS_TEXT.timeout}（本次预算 ${budgetSeconds}s）`
      : GRAPH_STATUS_TEXT[status];

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-memBorder-primary bg-surface-default-secondary px-3 py-2 text-xs">
      <span className="text-onSurface-default-tertiary">图分支</span>
      <span className={graphStatusClass(status)}>{status}</span>
      <span className="text-onSurface-default-secondary">{text}</span>
      <span className="text-onSurface-default-tertiary">
        图键{" "}
        <span className="font-mono text-onSurface-default-secondary">
          {graphKey ?? "--"}
        </span>
      </span>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="cursor-help text-onSurface-default-tertiary underline decoration-dotted underline-offset-2">
            口径
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">{GRAPH_KEY_NOTE}</TooltipContent>
      </Tooltip>
    </div>
  );
}
