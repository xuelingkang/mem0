"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { MEMORY_ENDPOINTS } from "@/utils/api-endpoints";
import {
  Memory,
  MemoryObservationsResponse,
  MemorySourcesResponse,
} from "@/types/api";
import {
  formatCount,
  formatDate,
  formatTimestamp,
  isObservation,
} from "@/utils/mechanism";
import { ScoreBreakdown } from "@/components/self-hosted/score-breakdown";
import { ValidityBadge } from "@/components/self-hosted/mechanism-badges";

interface MemoryDetailSheetProps {
  memory: Memory | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 打开关联记忆（被取代方 / 派生观察 / 源事实），沿用同一个 Sheet。 */
  onSelectMemory: (memory: Memory) => void;
  /** 删除入口：只有 memories 页传入；不传即本 Sheet 不提供写路径。 */
  onDelete?: (memory: Memory) => void;
  /** 检索模式下的本次图键（口径说明与分值构成同屏）。 */
  graphKey?: string | null;
}

/** 定义列表的一行。值为空时显示 `--`，不做任何推断。 */
function DetailRow({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-3 py-1.5">
      <span className="shrink-0 text-xs text-onSurface-default-tertiary">
        {label}
      </span>
      <span className="min-w-0 break-all text-right text-xs text-onSurface-default-primary">
        {value}
      </span>
    </div>
  );
}

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-onSurface-default-secondary">
        {title}
      </p>
      {hint && (
        <p className="text-[10px] text-onSurface-default-tertiary">{hint}</p>
      )}
      <div className="divide-y divide-memBorder-primary rounded-md border border-memBorder-primary px-3">
        {children}
      </div>
    </div>
  );
}

/**
 * 记忆 / 观察详情（右侧 Sheet）。
 *
 * 三段扩展到既有字段（Content / ID / User / Agent / Created）之后：bi-temporal、
 * 衰减足迹、双向证据链。既有字段位置与行为不动；本组件不发起任何写请求（删除按钮由
 * 调用方决定是否提供）。
 *
 * 双向追溯的两个端点都是**点击后**才发起（列表区不做逐行请求），且派生观察区只做
 * 定位与跳转——反向端点返回的观察行没有正文（只 6 个字段），就地展开也渲染不出内容。
 */
export function MemoryDetailSheet({
  memory,
  open,
  onOpenChange,
  onSelectMemory,
  onDelete,
  graphKey,
}: MemoryDetailSheetProps) {
  const [observedBy, setObservedBy] = useState<Memory[]>([]);
  const [observedTotal, setObservedTotal] = useState<number | null>(null);
  const [sources, setSources] = useState<Memory[]>([]);
  const [sourcesMeta, setSourcesMeta] = useState<{
    total: number;
    missing: number;
  } | null>(null);
  const [isLoadingLinks, setIsLoadingLinks] = useState(false);

  const memoryId = memory?.id ?? null;
  const observation = memory ? isObservation(memory) : false;

  useEffect(() => {
    if (!memoryId || !open) return;
    let active = true;
    setIsLoadingLinks(true);
    setObservedBy([]);
    setObservedTotal(null);
    setSources([]);
    setSourcesMeta(null);

    (async () => {
      try {
        if (observation) {
          const res = await api.get<MemorySourcesResponse>(
            MEMORY_ENDPOINTS.SOURCES_OF(memoryId),
          );
          if (!active) return;
          setSources(res.data?.results ?? []);
          setSourcesMeta({
            total: res.data?.total ?? 0,
            missing: res.data?.missing ?? 0,
          });
        } else {
          const res = await api.get<MemoryObservationsResponse>(
            MEMORY_ENDPOINTS.OBSERVATIONS_OF(memoryId),
          );
          if (!active) return;
          setObservedBy(res.data?.results ?? []);
          setObservedTotal(res.data?.total ?? 0);
        }
      } catch (error) {
        if (!active) return;
        toast({
          title: "Failed to load evidence chain",
          description: getErrorMessage(error),
          variant: "destructive",
        });
      } finally {
        if (active) setIsLoadingLinks(false);
      }
    })();

    return () => {
      active = false;
    };
  }, [memoryId, observation, open]);

  const openMemoryById = async (id: string) => {
    try {
      const res = await api.get<Memory>(MEMORY_ENDPOINTS.BY_ID(id));
      if (res.data) onSelectMemory(res.data);
    } catch (error) {
      toast({
        title: "Failed to open memory",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="sm:max-w-md overflow-y-auto">
        <SheetHeader>
          <SheetTitle>Memory Detail</SheetTitle>
          <SheetDescription className="sr-only">
            View memory content, mechanism attributes and evidence chain
          </SheetDescription>
        </SheetHeader>
        {memory && (
          <div className="mt-6 space-y-4">
            <div className="space-y-1">
              <Label className="text-xs text-onSurface-default-tertiary">
                Content
              </Label>
              <p className="text-sm">{memory.memory}</p>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1">
                <Label className="text-xs text-onSurface-default-tertiary">
                  ID
                </Label>
                <p className="text-xs font-mono break-all">{memory.id}</p>
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-onSurface-default-tertiary">
                  Status
                </Label>
                <div>
                  <ValidityBadge memory={memory} />
                </div>
              </div>
              {memory.user_id && (
                <div className="space-y-1">
                  <Label className="text-xs text-onSurface-default-tertiary">
                    User
                  </Label>
                  <p className="text-sm">{memory.user_id}</p>
                </div>
              )}
              {memory.agent_id && (
                <div className="space-y-1">
                  <Label className="text-xs text-onSurface-default-tertiary">
                    Agent
                  </Label>
                  <p className="text-sm">{memory.agent_id}</p>
                </div>
              )}
              {memory.created_at && (
                <div className="space-y-1">
                  <Label className="text-xs text-onSurface-default-tertiary">
                    Created
                  </Label>
                  <p className="text-sm">
                    {new Date(memory.created_at).toLocaleString()}
                  </p>
                </div>
              )}
            </div>

            <Section
              title="bi-temporal"
              hint="生效 / 失效四字段直译自响应；缺失即显示 --，不做推断。"
            >
              <DetailRow label="valid_at" value={formatDate(memory.valid_at)} />
              <DetailRow
                label="invalid_at"
                value={formatDate(memory.invalid_at)}
              />
              <DetailRow
                label="invalid_reason"
                value={memory.invalid_reason ?? "--"}
              />
              <DetailRow
                label="superseded_by"
                value={
                  memory.superseded_by ? (
                    <Button
                      variant="link"
                      className="h-auto p-0 font-mono text-xs"
                      onClick={() => void openMemoryById(memory.superseded_by!)}
                    >
                      {memory.superseded_by}
                    </Button>
                  ) : (
                    "--"
                  )
                }
              />
            </Section>

            <Section
              title="召回足迹（衰减输入）"
              hint="从未被检索返回过时两项均为 --。"
            >
              <DetailRow
                label="access_count"
                value={
                  memory.access_count == null
                    ? `${formatCount(memory.access_count)}（从未被检索返回过）`
                    : formatCount(memory.access_count)
                }
              />
              <DetailRow
                label="last_accessed"
                value={formatTimestamp(memory.last_accessed)}
              />
            </Section>

            {memory.score_details && (
              <ScoreBreakdown memory={memory} graphKey={graphKey} />
            )}

            {observation && (
              <Section title="观察身份">
                <DetailRow
                  label="observation_key"
                  value={
                    <span className="font-mono">
                      {memory.observation_key ?? "--"}
                    </span>
                  }
                />
                <DetailRow
                  label="evidence_count"
                  value={formatCount(memory.evidence_count)}
                />
                <DetailRow
                  label="dream_run_id"
                  value={
                    memory.dream_run_id ? (
                      <Link
                        href="/dashboard/dream"
                        className="font-mono text-xs underline underline-offset-4"
                      >
                        {memory.dream_run_id}
                      </Link>
                    ) : (
                      "--"
                    )
                  }
                />
                <DetailRow
                  label="source_memory_ids"
                  value={formatCount(memory.source_memory_ids?.length ?? null)}
                />
              </Section>
            )}

            {observation ? (
              <Section
                title="源事实（观察 → 事实）"
                hint={
                  sourcesMeta
                    ? `共 ${sourcesMeta.total} 条，缺失 ${sourcesMeta.missing} 条（缺失按端点口径原样呈现）`
                    : undefined
                }
              >
                {isLoadingLinks ? (
                  <p className="py-2 text-xs text-onSurface-default-tertiary">
                    Loading…
                  </p>
                ) : sources.length === 0 ? (
                  <p className="py-2 text-xs text-onSurface-default-tertiary">
                    没有可解析的源事实。
                  </p>
                ) : (
                  sources.map((source) => (
                    <button
                      key={source.id}
                      type="button"
                      className="flex w-full flex-col items-start gap-0.5 py-2 text-left"
                      onClick={() => onSelectMemory(source)}
                    >
                      <span className="font-mono text-[10px] text-onSurface-default-tertiary">
                        {source.id}
                      </span>
                      <span className="line-clamp-2 text-xs">
                        {source.memory}
                      </span>
                    </button>
                  ))
                )}
              </Section>
            ) : (
              <Section
                title="派生观察（事实 → 观察）"
                hint={
                  observedTotal !== null
                    ? `共 ${observedTotal} 条`
                    : "反向端点只返回观察的定位字段，点击后取全量再展开。"
                }
              >
                {isLoadingLinks ? (
                  <p className="py-2 text-xs text-onSurface-default-tertiary">
                    Loading…
                  </p>
                ) : observedBy.length === 0 ? (
                  <p className="py-2 text-xs text-onSurface-default-tertiary">
                    这条记忆没有派生出观察。
                  </p>
                ) : (
                  observedBy.map((row) => (
                    <button
                      key={row.id}
                      type="button"
                      className="flex w-full flex-col items-start gap-0.5 py-2 text-left"
                      onClick={() => void openMemoryById(row.id)}
                    >
                      <span className="font-mono text-[10px] text-onSurface-default-tertiary">
                        {row.observation_key ?? row.id}
                      </span>
                      <span className="text-xs">
                        证据 {formatCount(row.evidence_count)} 条 · 运行{" "}
                        <span className="font-mono">
                          {row.dream_run_id ?? "--"}
                        </span>
                      </span>
                    </button>
                  ))
                )}
              </Section>
            )}

            {onDelete && (
              <Button
                variant="outline"
                size="sm"
                className="text-onSurface-danger-primary"
                onClick={() => onDelete(memory)}
              >
                <Trash2 className="size-3.5 mr-1" />
                Delete memory
              </Button>
            )}
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
