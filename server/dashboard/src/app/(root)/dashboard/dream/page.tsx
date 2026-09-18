"use client";

import { useCallback, useEffect, useState } from "react";
import { format, formatDistanceToNow } from "date-fns";
import { RefreshCw, Sparkles, Loader2, Copy as CopyIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import { TruncatedText } from "@/components/misc/truncated-text";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { DREAM_ENDPOINTS, OBSERVATION_ENDPOINTS } from "@/utils/api-endpoints";
import {
  DreamPreviewReport,
  DreamRun,
  DreamRunsResponse,
  Memory,
  ObservationListResponse,
} from "@/types/api";
import { formatCount, formatDate, formatTimestamp } from "@/utils/mechanism";
import { MemoryDetailSheet } from "@/components/self-hosted/memory-detail-sheet";

const RUNS_PAGE_SIZE = 10;
const OBSERVATIONS_PAGE_SIZE = 10;

const statusClass = (status: string) => {
  switch (status) {
    case "completed":
      return "border-memGreen-200 bg-memGreen-50 text-onSurface-positive-primary";
    case "failed":
      return "border-memRed-200 bg-memRed-50 text-onSurface-danger-primary";
    case "timeout":
      return "border-memGold-200 bg-memGold-50 text-memGold-800";
    default:
      return "border-memBorder-primary bg-surface-default-tertiary text-onSurface-default-secondary";
  }
};

/**
 * Dream 页：运行审计 + 观察清单 + dry-run 预览。
 *
 * 三个分区共用一页，因为观察是运行的产物、`dream_run_id` 是它们之间的连接字段；
 * 分两页会让「这次运行产出了什么」需要两次跳转。
 *
 * 本页没有任何写路径：不提供「一键实跑」（`POST /dream/run` 会真正写入观察并可能取代
 * 既有事实，属于控制面），唯一的动作是 dry-run 预览——它零写入，唯一产物是一份报告文件。
 */
export default function DreamPage() {
  const [runs, setRuns] = useState<DreamRun[]>([]);
  const [runsTotal, setRunsTotal] = useState(0);
  const [runsPage, setRunsPage] = useState(0);
  const [isLoadingRuns, setIsLoadingRuns] = useState(true);

  const [observations, setObservations] = useState<Memory[]>([]);
  const [observationsTotal, setObservationsTotal] = useState<number | null>(
    null,
  );
  const [observationCursor, setObservationCursor] = useState<string | null>(
    null,
  );
  const [observationPage, setObservationPage] = useState(0);
  const [isLoadingObservations, setIsLoadingObservations] = useState(true);
  const [selectedObservation, setSelectedObservation] = useState<Memory | null>(
    null,
  );

  const [previewOpen, setPreviewOpen] = useState(false);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [preview, setPreview] = useState<DreamPreviewReport | null>(null);

  const runsTotalPages = Math.max(1, Math.ceil(runsTotal / RUNS_PAGE_SIZE));
  const observationTotalPages = Math.max(
    1,
    Math.ceil(observations.length / OBSERVATIONS_PAGE_SIZE),
  );
  const paginatedObservations = observations.slice(
    observationPage * OBSERVATIONS_PAGE_SIZE,
    (observationPage + 1) * OBSERVATIONS_PAGE_SIZE,
  );

  const fetchRuns = useCallback(async (page: number) => {
    setIsLoadingRuns(true);
    try {
      const res = await api.get<DreamRunsResponse>(DREAM_ENDPOINTS.RUNS, {
        params: { limit: RUNS_PAGE_SIZE, offset: page * RUNS_PAGE_SIZE },
      });
      setRuns(res.data?.results ?? []);
      setRunsTotal(res.data?.total ?? 0);
    } catch (error) {
      toast({
        title: "Failed to load dream runs",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    } finally {
      setIsLoadingRuns(false);
    }
  }, []);

  const fetchObservations = useCallback(
    async (cursor: string | null, append: boolean) => {
      setIsLoadingObservations(true);
      try {
        const params: Record<string, string | number> = {
          top_k: OBSERVATIONS_PAGE_SIZE,
        };
        if (cursor) params.cursor = cursor;
        const res = await api.get<ObservationListResponse>(
          OBSERVATION_ENDPOINTS.BASE,
          { params },
        );
        const rows = res.data?.results ?? [];
        setObservationCursor(res.data?.next_cursor ?? null);
        if (typeof res.data?.total === "number")
          setObservationsTotal(res.data.total);
        setObservations((prev) => (append ? [...prev, ...rows] : rows));
      } catch (error) {
        toast({
          title: "Failed to load observations",
          description: getErrorMessage(error),
          variant: "destructive",
        });
      } finally {
        setIsLoadingObservations(false);
      }
    },
    [],
  );

  useEffect(() => {
    void fetchRuns(0);
    void fetchObservations(null, false);
  }, [fetchRuns, fetchObservations]);

  const runPreview = async () => {
    setIsPreviewing(true);
    try {
      const res = await api.post<DreamPreviewReport>(
        DREAM_ENDPOINTS.PREVIEW,
        {},
      );
      setPreview(res.data ?? null);
      toast({
        title: "Preview complete (no data written)",
        variant: "success",
      });
    } catch (error) {
      // 409 的 detail 原文即是两种拒绝语义（另一轮整合进行中 / Dream 已关闭）。
      toast({
        title: "Preview rejected",
        description: getErrorMessage(error),
        variant: "destructive",
      });
      setPreviewOpen(false);
    } finally {
      setIsPreviewing(false);
    }
  };

  const runsColumns = [
    {
      key: "started_at" as keyof DreamRun,
      label: "Started",
      width: 150,
      render: (value: string | null) =>
        value ? format(new Date(value), "MMM d, HH:mm:ss") : "--",
    },
    {
      key: "finished_at" as keyof DreamRun,
      label: "Finished",
      width: 150,
      render: (value: string | null) =>
        value ? format(new Date(value), "MMM d, HH:mm:ss") : "--",
    },
    {
      key: "duration_seconds" as keyof DreamRun,
      label: "Duration",
      width: 90,
      align: "right" as const,
      render: (value: number | null) =>
        typeof value === "number" ? `${value.toFixed(1)}s` : "--",
    },
    {
      key: "mode" as keyof DreamRun,
      label: "Mode",
      width: 80,
      render: (value: string) => (
        <Badge variant="outline" className="capitalize">
          {value}
        </Badge>
      ),
    },
    {
      key: "status" as keyof DreamRun,
      label: "Status",
      width: 100,
      render: (value: string) => (
        <span
          className={`inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium ${statusClass(
            value,
          )}`}
        >
          {value}
        </span>
      ),
    },
    {
      key: "scopes" as keyof DreamRun,
      label: "Scopes",
      width: 70,
      align: "right" as const,
    },
    {
      key: "clusters" as keyof DreamRun,
      label: "Clusters",
      width: 80,
      align: "right" as const,
    },
    {
      key: "observations_written" as keyof DreamRun,
      label: "Written",
      width: 80,
      align: "right" as const,
    },
    {
      key: "observations_superseded" as keyof DreamRun,
      label: "Superseded",
      width: 90,
      align: "right" as const,
    },
    {
      key: "failed_clusters" as keyof DreamRun,
      label: "Failed",
      width: 70,
      align: "right" as const,
      render: (value: number) => (
        <span className={value > 0 ? "text-onSurface-danger-primary" : ""}>
          {value}
        </span>
      ),
    },
    {
      key: "llm_calls" as keyof DreamRun,
      label: "LLM calls",
      width: 80,
      align: "right" as const,
    },
    {
      key: "report_path" as keyof DreamRun,
      label: "Report",
      width: 240,
      render: (value: string | null) =>
        value ? (
          <span className="flex items-center gap-2">
            <span className="truncate font-mono text-[10px]" title={value}>
              {value}
            </span>
            <button
              type="button"
              className="shrink-0 text-onSurface-default-tertiary hover:text-onSurface-default-secondary"
              onClick={(event) => {
                event.stopPropagation();
                void navigator.clipboard?.writeText(value);
                toast({ title: "Report path copied", variant: "success" });
              }}
              aria-label="Copy report path"
            >
              <CopyIcon className="size-3" />
            </button>
          </span>
        ) : (
          "--"
        ),
    },
  ];

  const observationColumns = [
    {
      key: "memory" as keyof Memory,
      label: "Observation",
      width: 420,
      render: (value: string) => <TruncatedText text={value} limit={90} />,
    },
    {
      key: "evidence_count" as keyof Memory,
      label: "Evidence",
      width: 80,
      align: "right" as const,
      render: (value: number | null | undefined) => formatCount(value),
    },
    {
      key: "source_memory_ids" as keyof Memory,
      label: "Sources",
      width: 80,
      align: "right" as const,
      render: (value: string[] | null | undefined) =>
        formatCount(value?.length),
    },
    {
      key: "dream_run_id" as keyof Memory,
      label: "Run",
      width: 120,
      render: (value: string | null | undefined) => (
        <span className="truncate font-mono text-[10px]">{value ?? "--"}</span>
      ),
    },
    {
      key: "valid_at" as keyof Memory,
      label: "Valid at",
      width: 100,
      render: (value: string | null | undefined) => formatDate(value),
    },
    {
      key: "invalid_at" as keyof Memory,
      label: "Status",
      width: 100,
      render: (value: string | null | undefined, row: Memory) => (
        <span
          className={
            row.invalid_at
              ? "text-xs text-onSurface-default-tertiary"
              : "text-xs text-onSurface-default-secondary"
          }
        >
          {row.invalid_at ? "已失效" : "有效"}
        </span>
      ),
    },
  ];

  const skipReasons = (preview?.candidates ?? []).reduce<
    Record<string, number>
  >((acc, candidate) => {
    if (candidate.decision === "would_write") return acc;
    const reason = candidate.skip_reason || "unspecified";
    acc[reason] = (acc[reason] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold font-fustat">Dream</h1>
          <p className="text-sm text-onSurface-default-secondary">
            Background memory consolidation: run audit, synthesized observations
            and their evidence chains.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            onClick={() => {
              void fetchRuns(runsPage);
              void fetchObservations(null, false);
              setObservationPage(0);
            }}
          >
            <RefreshCw className="size-4 mr-2" />
            Refresh
          </Button>
          <Button onClick={() => setPreviewOpen(true)} disabled={isPreviewing}>
            <Sparkles className="size-4 mr-2" />
            预览整合（dry-run）
          </Button>
        </div>
      </div>

      <Card className="border-memBorder-primary">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Runs</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoadingRuns ? (
            <div className="p-4">
              <TableSkeleton rows={3} columns={6} />
            </div>
          ) : runs.length === 0 ? (
            <EmptyState
              title="No dream runs yet"
              description="A run record appears after the first live consolidation. The dry-run preview below never adds a row."
              image="requests"
            />
          ) : (
            <>
              <DataTable
                data={runs}
                columns={runsColumns}
                getRowKey={(row) => row.id}
              />
              <div className="flex items-center justify-between px-4 py-3 text-sm text-onSurface-default-tertiary">
                <span>
                  {runsPage * RUNS_PAGE_SIZE + 1}–
                  {Math.min((runsPage + 1) * RUNS_PAGE_SIZE, runsTotal)} of{" "}
                  {runsTotal}
                </span>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={runsPage === 0}
                    onClick={() => {
                      const next = runsPage - 1;
                      setRunsPage(next);
                      void fetchRuns(next);
                    }}
                  >
                    Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={runsPage >= runsTotalPages - 1}
                    onClick={() => {
                      const next = runsPage + 1;
                      setRunsPage(next);
                      void fetchRuns(next);
                    }}
                  >
                    Next
                  </Button>
                </div>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      <Card className="border-memBorderPrimary">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">
            Observations
            {observationsTotal !== null && (
              <span className="ml-2 text-xs font-normal text-onSurface-default-tertiary">
                {formatCount(observationsTotal)} total
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoadingObservations && observations.length === 0 ? (
            <div className="p-4">
              <TableSkeleton rows={3} columns={5} />
            </div>
          ) : observations.length === 0 ? (
            <EmptyState
              title="No observations yet"
              description="Observations appear once a live consolidation writes synthesized beliefs."
            />
          ) : (
            <>
              <DataTable
                data={paginatedObservations}
                columns={observationColumns}
                getRowKey={(row) => row.id}
                onRowClick={(row) => setSelectedObservation(row)}
              />
              <div className="flex items-center justify-between px-4 py-3 text-sm text-onSurface-default-tertiary">
                <span>
                  {observationPage * OBSERVATIONS_PAGE_SIZE + 1}–
                  {Math.min(
                    (observationPage + 1) * OBSERVATIONS_PAGE_SIZE,
                    observations.length,
                  )}{" "}
                  of {observationsTotal ?? `${observations.length}+`}
                </span>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={observationPage === 0}
                    onClick={() => setObservationPage((p) => p - 1)}
                  >
                    Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={
                      observationPage >= observationTotalPages - 1 &&
                      !observationCursor
                    }
                    onClick={() => {
                      if (observationPage < observationTotalPages - 1) {
                        setObservationPage((p) => p + 1);
                      } else if (observationCursor) {
                        void fetchObservations(observationCursor, true);
                        setObservationPage((p) => p + 1);
                      }
                    }}
                  >
                    Next
                  </Button>
                </div>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      <MemoryDetailSheet
        memory={selectedObservation}
        open={!!selectedObservation}
        onOpenChange={(open) => {
          if (!open) setSelectedObservation(null);
        }}
        onSelectMemory={(row) => setSelectedObservation(row)}
      />

      <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogTitle>预览整合（dry-run）</DialogTitle>
          <DialogDescription className="mb-4">
            {`本轮会走完整四阶段判定，但不写入任何数据：不落 dream_runs 行、不写 Qdrant、不写状态表。它唯一的产物是一份报告文件，并且与实跑共用同一把锁。`}
          </DialogDescription>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setPreviewOpen(false)}>
              Cancel
            </Button>
            <Button onClick={() => void runPreview()} disabled={isPreviewing}>
              {isPreviewing && <Loader2 className="size-4 mr-2 animate-spin" />}
              {isPreviewing ? "预览中…" : "开始预览"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {preview && (
        <Card className="border-memBorderPrimary">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">
              预览报告
              <span className="ml-2 text-xs font-normal text-onSurface-default-tertiary">
                {preview.run_id}
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 p-4 pt-0">
            <p className="text-xs text-onSurface-positive-primary">
              本轮预览未写入任何数据。
            </p>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              {[
                {
                  label: "候选簇",
                  value: formatCount(preview.totals.clusters),
                },
                {
                  label: "会写入",
                  value: formatCount(preview.would_write?.length ?? 0),
                },
                {
                  label: "跳过簇",
                  value: formatCount(preview.totals.clusters_skipped),
                },
                {
                  label: "延后簇",
                  value: formatCount(preview.totals.clusters_deferred),
                },
                {
                  label: "LLM 调用",
                  value: formatCount(preview.totals.llm_calls),
                },
                {
                  label: "失败簇",
                  value: formatCount(preview.totals.failed_clusters),
                },
                {
                  label: "耗时",
                  value: `${preview.duration_seconds.toFixed(1)}s`,
                },
                {
                  label: "tokens",
                  value: formatCount(
                    preview.totals.prompt_tokens +
                      preview.totals.completion_tokens,
                  ),
                },
              ].map((card) => (
                <div
                  key={card.label}
                  className="rounded-md border border-memBorder-primary p-3"
                >
                  <p className="text-xs text-onSurface-default-tertiary">
                    {card.label}
                  </p>
                  <p className="mt-1 text-lg font-semibold">{card.value}</p>
                </div>
              ))}
            </div>
            {Object.keys(skipReasons).length > 0 && (
              <div className="space-y-1">
                <p className="text-xs text-onSurface-default-secondary">
                  跳过原因分布
                </p>
                <div className="divide-y divide-memBorder-primary rounded-md border border-memBorder-primary">
                  {Object.entries(skipReasons).map(([reason, count]) => (
                    <div
                      key={reason}
                      className="flex items-center justify-between px-3 py-1.5 text-xs"
                    >
                      <span className="font-mono">{reason}</span>
                      <span>{formatCount(count)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <p className="text-xs text-onSurface-default-tertiary">
              完成于{" "}
              {formatDistanceToNow(
                new Date(preview.finished_at ?? preview.started_at),
                {
                  addSuffix: true,
                },
              )}
              ；报告路径：
              <span className="font-mono">{preview.report_path ?? "--"}</span>
            </p>
            {preview.errors.length > 0 && (
              <p className="text-xs text-onSurface-danger-primary">
                {preview.errors.length} 个簇判定失败（见报告）
              </p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
