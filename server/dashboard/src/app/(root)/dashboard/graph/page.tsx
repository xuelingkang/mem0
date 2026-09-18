"use client";

import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { GRAPH_ENDPOINTS } from "@/utils/api-endpoints";
import { GraphKeysResponse, GraphKeyScale, GraphStats } from "@/types/api";
import { formatCount, PROCESS_COUNTER_NOTE } from "@/utils/mechanism";

// 进程内计数的五个字段：派发 / 入图成功 / 幂等跳过 / 失败 / 丢弃。
const COUNTER_FIELDS: { key: keyof GraphStats; label: string }[] = [
  { key: "graph_dispatched", label: "派发 dispatched" },
  { key: "graph_synced", label: "入图成功 synced" },
  { key: "graph_already_synced", label: "幂等跳过 already_synced" },
  { key: "graph_failed", label: "失败 failed" },
  { key: "graph_dropped", label: "丢弃 dropped" },
];

const isEmptyGraphKey = (key: GraphKeyScale): boolean =>
  key.episodes === 0 && key.entity_nodes === 0 && key.entity_edges === 0;

/**
 * Graph 页：图能力的派发计数与图键规模。
 *
 * 两个分区来自**两个不同的存储**：计数是 mem0 进程内的累计值（换进程即归零，因此必须
 * 标注「自本进程启动以来」，否则「重启归零」会被读成数据丢失）；图键规模只能由图桥提供，
 * 经 mem0 侧的只读代理取得（图桥无鉴权且未映射宿主端口，前端不得直连）。
 *
 * 本页不提供任何写入口：不删图键、不派发、不建键。
 */
export default function GraphPage() {
  const [stats, setStats] = useState<GraphStats | null>(null);
  const [isLoadingStats, setIsLoadingStats] = useState(true);
  const [statsError, setStatsError] = useState("");

  const [keys, setKeys] = useState<GraphKeyScale[]>([]);
  const [degraded, setDegraded] = useState(false);
  const [isLoadingKeys, setIsLoadingKeys] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);

  const fetchStats = async () => {
    setIsLoadingStats(true);
    setStatsError("");
    try {
      const res = await api.get<GraphStats>(GRAPH_ENDPOINTS.STATS);
      setStats(res.data ?? null);
    } catch (error) {
      setStatsError(getErrorMessage(error));
    } finally {
      setIsLoadingStats(false);
    }
  };

  const fetchKeys = async () => {
    setIsLoadingKeys(true);
    try {
      const res = await api.get<GraphKeysResponse>(GRAPH_ENDPOINTS.KEYS);
      setKeys(res.data?.keys ?? []);
      setDegraded(res.data?.degraded ?? false);
    } catch (error) {
      // 代理端点本身失败：与「图桥不可用」同义——读数失败，而不是规模为 0。
      setKeys([]);
      setDegraded(true);
      toast({
        title: "Failed to load graph keys",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    } finally {
      setIsLoadingKeys(false);
    }
  };

  useEffect(() => {
    void fetchStats();
    void fetchKeys();
  }, []);

  const refresh = async () => {
    setIsRefreshing(true);
    await Promise.all([fetchStats(), fetchKeys()]);
    setIsRefreshing(false);
  };

  const keysColumns = [
    {
      key: "group_id" as keyof GraphKeyScale,
      label: "Graph key",
      width: 220,
      render: (value: string) => (
        <span className="font-mono text-xs">{value}</span>
      ),
    },
    {
      key: "episodes" as keyof GraphKeyScale,
      label: "Memories (episodes)",
      width: 140,
      align: "right" as const,
    },
    {
      key: "entity_nodes" as keyof GraphKeyScale,
      label: "Entity nodes",
      width: 120,
      align: "right" as const,
    },
    {
      key: "entity_edges" as keyof GraphKeyScale,
      label: "Entity edges",
      width: 120,
      align: "right" as const,
    },
    {
      key: "group_id" as keyof GraphKeyScale,
      label: "判定",
      width: 110,
      render: (_value: string, row: GraphKeyScale) =>
        isEmptyGraphKey(row) ? (
          <span className="text-xs text-onSurface-default-tertiary">
            空图键（三项规模均为 0）
          </span>
        ) : (
          <span className="text-xs text-onSurface-default-secondary">
            有规模
          </span>
        ),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold font-fustat">Graph</h1>
          <p className="text-sm text-onSurface-default-secondary">
            Graph side-path: dispatch counters (this process) and per-graph-key
            size.
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => void refresh()}
          disabled={isRefreshing}
        >
          <RefreshCw className="size-4 mr-2" />
          Refresh
        </Button>
      </div>

      {statsError && (
        <Card className="border-memBorderPrimary">
          <CardContent className="p-4 text-sm text-onSurface-danger-primary">
            {statsError}
          </CardContent>
        </Card>
      )}

      {isLoadingStats && !stats ? (
        <TableSkeleton rows={4} columns={4} />
      ) : stats ? (
        <>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
            <Card className="border-memBorderPrimary">
              <CardContent className="p-5">
                <p className="text-xs text-onSurface-default-tertiary">
                  图能力 enabled
                </p>
                <p className="mt-1 text-2xl font-semibold">
                  {stats.enabled ? "true" : "false"}
                </p>
              </CardContent>
            </Card>
            <Card className="border-memBorderPrimary">
              <CardContent className="p-5">
                <p className="text-xs text-onSurface-default-tertiary">
                  检索预算 timeout_seconds
                </p>
                <p className="mt-1 text-2xl font-semibold">
                  {stats.timeout_seconds ?? "--"}
                </p>
                <p className="mt-1 text-[10px] text-onSurface-default-tertiary">
                  超出预算的图检索会被记为 timeout
                </p>
              </CardContent>
            </Card>
            <Card className="border-memBorderPrimary">
              <CardContent className="p-5">
                <p className="text-xs text-onSurface-default-tertiary">
                  队列长度 queue_size（瞬时值）
                </p>
                <p className="mt-1 text-2xl font-semibold">
                  {formatCount(stats.queue_size)}
                </p>
              </CardContent>
            </Card>
            <Card
              className={
                stats.circuit_open
                  ? "border-memRed-200 bg-memRed-50"
                  : "border-memBorderPrimary"
              }
            >
              <CardContent className="p-5">
                <p className="text-xs text-onSurface-default-tertiary">
                  熔断 circuit_open / 连续失败
                </p>
                <p className="mt-1 text-2xl font-semibold">
                  {stats.circuit_open ? "open" : "closed"}
                  <span className="ml-2 text-base font-normal text-onSurface-default-secondary">
                    {formatCount(stats.consecutive_failures)}
                  </span>
                </p>
              </CardContent>
            </Card>
          </div>

          <Card className="border-memBorderPrimary">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">
                派发计数
                <span className="ml-2 text-xs font-normal text-onSurface-default-tertiary">
                  {PROCESS_COUNTER_NOTE}
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-0">
              <div className="divide-y divide-memBorder-primary rounded-md border border-memBorderPrimary">
                {COUNTER_FIELDS.map((field) => (
                  <div
                    key={field.key}
                    className="flex items-center justify-between px-3 py-2 text-sm"
                  >
                    <span className="text-onSurface-default-secondary">
                      {field.label}
                    </span>
                    <span className="font-mono">
                      {formatCount(stats[field.key] as number)}
                    </span>
                  </div>
                ))}
              </div>
              <p className="mt-2 text-[10px] text-onSurface-default-tertiary">
                计数是进程内累计值：换一个进程实例即从 0
                起（不是数据丢失，也不是全库口径）。
              </p>
            </CardContent>
          </Card>
        </>
      ) : null}

      <Card className="border-memBorderPrimary">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">图键与规模</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoadingKeys && keys.length === 0 && !degraded ? (
            <div className="p-4">
              <TableSkeleton rows={3} columns={5} />
            </div>
          ) : degraded ? (
            <div className="p-4">
              <Alert
                variant="destructive"
                className="border-memGold-200 bg-memGold-50"
              >
                <AlertTitle>图桥无响应</AlertTitle>
                <AlertDescription>
                  规模读数失败（连线失败 / 非 2xx /
                  单个键读失败）。这不是「规模为 0」，
                  两者语义不同；恢复图桥后刷新即可。
                </AlertDescription>
              </Alert>
            </div>
          ) : keys.length === 0 ? (
            <EmptyState
              title="No graph keys"
              description="Graph keys appear once facts are dispatched into the graph."
            />
          ) : (
            <DataTable
              data={keys}
              columns={keysColumns}
              getRowKey={(row) => row.group_id}
            />
          )}
          <p className="px-4 pb-4 pt-2 text-[10px] text-onSurface-default-tertiary">
            图键由作用域派生（user_id 优先 ⇒ mem0_&lt;user_id&gt;），同一
            user_id 的不同 agent_id 共用一个图键——因此规模不是「按
            agent」的维度。检索模式下 graph_boost = 0
            只有在候选池里没有任何条目被图事实引用时出现，需与本次检索的图键
            一起读。
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
