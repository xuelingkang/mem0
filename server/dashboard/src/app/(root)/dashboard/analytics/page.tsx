"use client";

import { useEffect, useRef, useState } from "react";
import { formatDistanceToNow } from "date-fns";
import { RefreshCw } from "lucide-react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/self-hosted/empty-state";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { api } from "@/utils/api";
import { REQUEST_ENDPOINTS } from "@/utils/api-endpoints";
import { useApiQuery } from "@/hooks/use-api-query";
import { RequestStats } from "@/types/api";

const WINDOW_OPTIONS = [7, 14, 30] as const;

const CHART = {
  operations: "var(--mem-purple-500)",
  errors: "var(--mem-red-500)",
  grid: "var(--mem-border-primary)",
  axis: "var(--on-surface-default-tertiary)",
};

const tooltipStyle = {
  background: "var(--surface-default-primary)",
  border: "1px solid var(--mem-border-primary)",
  borderRadius: 8,
  fontSize: 12,
  color: "var(--on-surface-default-primary)",
};

const statusClassName = (statusCode: number) => {
  if (statusCode >= 500) return "text-onSurface-default-primary";
  if (statusCode >= 400) return "text-onSurface-default-secondary";
  return "text-onSurface-default-secondary";
};

export default function AnalyticsPage() {
  const [days, setDays] = useState<number>(14);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const isFirstRender = useRef(true);

  const {
    data: stats,
    isLoading,
    error,
    refetch,
  } = useApiQuery<RequestStats>(
    async () => {
      const res = await api.get<RequestStats>(REQUEST_ENDPOINTS.STATS, {
        params: { days },
      });
      setLastUpdated(new Date().toISOString());
      return res.data;
    },
    { errorToast: "Failed to load analytics" },
  );

  useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false;
      return;
    }
    void refetch();
  }, [days, refetch]);

  const hasData = Boolean(stats && stats.total > 0);
  const statusTotal =
    stats?.by_status.reduce((sum, row) => sum + row.count, 0) ?? 0;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold font-fustat">Analytics</h1>
          <p className="text-sm text-onSurface-default-secondary">
            Memory operations, latency, and error patterns over time.
          </p>
          {lastUpdated && (
            <p className="text-xs text-onSurface-default-tertiary">
              Last updated{" "}
              {formatDistanceToNow(new Date(lastUpdated), { addSuffix: true })}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-md border border-memBorder-primary p-0.5">
            {WINDOW_OPTIONS.map((option) => (
              <Button
                key={option}
                variant={days === option ? "default" : "ghost"}
                size="sm"
                className="h-7 px-2.5 text-xs"
                onClick={() => setDays(option)}
              >
                {option}d
              </Button>
            ))}
          </div>
          <Button
            variant="outline"
            onClick={() => void refetch()}
            disabled={isLoading}
          >
            <RefreshCw className="size-4 mr-2" />
            Refresh
          </Button>
        </div>
      </div>

      {error && (
        <Card className="border-memBorder-primary">
          <CardContent className="p-4 text-sm text-onSurface-danger-primary">
            {error}
          </CardContent>
        </Card>
      )}

      {isLoading && !stats ? (
        <TableSkeleton rows={6} columns={3} />
      ) : !hasData ? (
        <EmptyState
          title="No activity in this window"
          description="Analytics appear once your instance records API requests."
          image="requests"
        />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
            {[
              { label: `Operations (${days}d)`, value: stats!.total },
              { label: "Success Rate", value: `${stats!.success_rate}%` },
              {
                label: "Avg Latency",
                value: `${(stats!.avg_latency_ms / 1000).toFixed(2)} s`,
              },
              {
                label: "P95 Latency",
                value: `${(stats!.p95_latency_ms / 1000).toFixed(2)} s`,
              },
            ].map((card) => (
              <Card key={card.label} className="border-memBorder-primary">
                <CardContent className="p-5">
                  <p className="text-xs text-onSurface-default-tertiary">
                    {card.label}
                  </p>
                  <p className="mt-1 text-2xl font-semibold">{card.value}</p>
                </CardContent>
              </Card>
            ))}
          </div>

          <Card className="border-memBorder-primary">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">
                Operations over time
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-0">
              <div className="h-[240px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart
                    data={stats!.by_day}
                    margin={{ top: 8, right: 8, left: -16, bottom: 0 }}
                  >
                    <defs>
                      <linearGradient id="opsFill" x1="0" y1="0" x2="0" y2="1">
                        <stop
                          offset="0%"
                          stopColor={CHART.operations}
                          stopOpacity={0.35}
                        />
                        <stop
                          offset="100%"
                          stopColor={CHART.operations}
                          stopOpacity={0.02}
                        />
                      </linearGradient>
                    </defs>
                    <CartesianGrid
                      stroke={CHART.grid}
                      strokeDasharray="3 3"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="date"
                      tickFormatter={(value: string) => value.slice(5)}
                      stroke={CHART.axis}
                      fontSize={11}
                      tickLine={false}
                    />
                    <YAxis stroke={CHART.axis} fontSize={11} tickLine={false} />
                    <Tooltip contentStyle={tooltipStyle} />
                    <Area
                      type="monotone"
                      dataKey="total"
                      name="operations"
                      stroke={CHART.operations}
                      strokeWidth={2}
                      fill="url(#opsFill)"
                    />
                    <Area
                      type="monotone"
                      dataKey="errors"
                      name="errors"
                      stroke={CHART.errors}
                      strokeWidth={2}
                      fill="none"
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </CardContent>
          </Card>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Card className="border-memBorder-primary">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Top endpoints</CardTitle>
              </CardHeader>
              <CardContent className="p-4 pt-0">
                <div className="h-[260px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart
                      data={stats!.top_paths}
                      layout="vertical"
                      margin={{ top: 8, right: 16, left: 8, bottom: 0 }}
                    >
                      <CartesianGrid
                        stroke={CHART.grid}
                        strokeDasharray="3 3"
                        horizontal={false}
                      />
                      <XAxis
                        type="number"
                        stroke={CHART.axis}
                        fontSize={11}
                        tickLine={false}
                      />
                      <YAxis
                        type="category"
                        dataKey="path"
                        width={140}
                        stroke={CHART.axis}
                        fontSize={11}
                        tickLine={false}
                        tickFormatter={(value: string) =>
                          value.length > 22
                            ? `${value.slice(0, 21)}…`
                            : value
                        }
                      />
                      <Tooltip contentStyle={tooltipStyle} />
                      <Bar
                        dataKey="count"
                        name="requests"
                        fill={CHART.operations}
                        radius={[0, 4, 4, 0]}
                      />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </CardContent>
            </Card>

            <Card className="border-memBorder-primary">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Response status</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 p-4 pt-2">
                {stats!.by_status.map((row) => {
                  const share =
                    statusTotal > 0 ? (row.count / statusTotal) * 100 : 0;
                  const isError = row.status_code >= 400;
                  return (
                    <div key={row.status_code} className="space-y-1.5">
                      <div className="flex items-center justify-between text-xs">
                        <span
                          className={`font-mono ${statusClassName(
                            row.status_code,
                          )}`}
                        >
                          {row.status_code}
                        </span>
                        <span className="text-onSurface-default-secondary">
                          {row.count.toLocaleString()} · {share.toFixed(1)}%
                        </span>
                      </div>
                      <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-default-secondary">
                        <div
                          className="h-full rounded-full"
                          style={{
                            width: `${share}%`,
                            background: isError
                              ? CHART.errors
                              : CHART.operations,
                          }}
                        />
                      </div>
                    </div>
                  );
                })}
              </CardContent>
            </Card>
          </div>

          <Card className="border-memBorder-primary">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">
                Avg latency by endpoint
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <div className="divide-y divide-memBorder-primary">
                {stats!.top_paths.map((row) => (
                  <div
                    key={row.path}
                    className="flex items-center justify-between gap-4 px-4 py-2.5 text-xs"
                  >
                    <span className="font-mono break-all text-onSurface-default-primary">
                      {row.path}
                    </span>
                    <span className="shrink-0 text-onSurface-default-secondary">
                      {(row.avg_latency_ms / 1000).toFixed(2)} s avg ·{" "}
                      {row.count.toLocaleString()} calls
                    </span>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
