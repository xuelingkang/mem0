"use client";

import { useEffect, useState } from "react";
import { Trash2, Search } from "lucide-react";
import { format } from "date-fns";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { GRAPH_ENDPOINTS, MEMORY_ENDPOINTS } from "@/utils/api-endpoints";
import {
  GraphStats,
  GraphStatus,
  Memory,
  MemoryListResponse,
} from "@/types/api";
import { formatCount, formatScore, isObservation } from "@/utils/mechanism";
import { deriveGraphKey } from "@/utils/graph-key";
import {
  GraphStatusBar,
  ObservationBadge,
  ValidityBadge,
} from "@/components/self-hosted/mechanism-badges";
import { MemoryDetailSheet } from "@/components/self-hosted/memory-detail-sheet";

// Page size for cursor pagination. The backend returns { results, next_cursor } —
// we keep a page buffer and append further pages as the user navigates.
const PAGE_SIZE = 10;
const CURSOR_FETCH_LIMIT = 10;
// 检索模式的作用域：通配（管理面检索），与既有行为一致。图键即由它派生出 mem0__。
const SEARCH_FILTERS = { user_id: "*" };

export default function MemoriesPage() {
  const [query, setQuery] = useState("");
  const [searchMode, setSearchMode] = useState(false);
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);
  const [memoryToDelete, setMemoryToDelete] = useState<Memory | null>(null);
  // Cursor pagination state: keep all fetched rows so the table can page both ways
  // without re-fetching older data (server-side keyset cursor only moves forward).
  const [memories, setMemories] = useState<Memory[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [page, setPage] = useState(0);
  // 检索模式的机制透明：两个既有请求参数 + 图分支状态 + 生效预算。
  const [includeObservations, setIncludeObservations] = useState(false);
  const [includeInvalidated, setIncludeInvalidated] = useState(false);
  const [graphStatus, setGraphStatus] = useState<GraphStatus | null>(null);
  const [graphBudgetSeconds, setGraphBudgetSeconds] = useState<number | null>(
    null,
  );
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";
  const searchGraphKey = deriveGraphKey(SEARCH_FILTERS);

  // Browse mode: cursor-paginated GET /memories (newest first).
  const fetchBrowsePage = async (cursor: string | null) => {
    const params: Record<string, string | number> = {
      top_k: CURSOR_FETCH_LIMIT,
    };
    if (cursor) params.cursor = cursor;
    const res = await api.get<MemoryListResponse>(MEMORY_ENDPOINTS.BASE, {
      params,
    });
    const raw = res.data?.results ?? res.data ?? [];
    const rows: Memory[] = Array.isArray(raw) ? raw : [];
    setNextCursor(res.data?.next_cursor ?? null);
    setHasMore(res.data?.has_more === true || !!res.data?.next_cursor);
    if (typeof res.data?.total === "number") setTotalCount(res.data.total);
    return rows;
  };

  // Search mode: semantic (vector) recall via POST /search over the whole collection.
  // 恒定带 explain=true：分量透明是本次必做项，做成开关会让默认视图没有分量。
  const fetchSearchPage = async (
    q: string,
    options?: { includeObservations?: boolean; includeInvalidated?: boolean },
  ) => {
    const res = await api.post(MEMORY_ENDPOINTS.SEARCH, {
      query: q,
      top_k: 50,
      filters: SEARCH_FILTERS,
      explain: true,
      include_observations: options?.includeObservations ?? includeObservations,
      include_invalidated: options?.includeInvalidated ?? includeInvalidated,
    });
    const raw = res.data?.results ?? res.data ?? [];
    const rows = (Array.isArray(raw) ? raw : []) as Memory[];
    // graph_status 在同一响应内恒定；取首条即整次检索的图分支状态。
    setGraphStatus(rows[0]?.graph_status ?? null);
    return rows;
  };

  // 生效的图检索预算用于解释「为什么这次是 timeout」，来自既有计数端点。
  const fetchGraphBudget = async () => {
    try {
      const res = await api.get<GraphStats>(GRAPH_ENDPOINTS.STATS);
      setGraphBudgetSeconds(res.data?.timeout_seconds ?? null);
    } catch {
      setGraphBudgetSeconds(null);
    }
  };

  const loadInitial = async () => {
    setIsLoading(true);
    try {
      const q = query.trim();
      const isSearch = q.length > 0;
      setSearchMode(isSearch);
      if (isSearch) {
        setMemories(await fetchSearchPage(q));
        setTotalCount(null);
        // Search results are a one-shot ranked list — no server-side pagination.
        setNextCursor(null);
        setHasMore(false);
        void fetchGraphBudget();
      } else {
        setGraphStatus(null);
        setMemories(await fetchBrowsePage(null));
      }
      setPage(0);
    } catch (error) {
      toast({
        title: "Failed to load memories",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  };

  const loadMore = async () => {
    if (searchMode || !nextCursor || isLoadingMore) return;
    setIsLoadingMore(true);
    try {
      const more = await fetchBrowsePage(nextCursor);
      setMemories((prev: Memory[]) => [...prev, ...more]);
    } catch (error) {
      toast({
        title: "Failed to load more memories",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    } finally {
      setIsLoadingMore(false);
    }
  };

  // Load first page on mount.
  useEffect(() => {
    void loadInitial();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") void loadInitial();
  };

  const totalPages = Math.max(1, Math.ceil(memories.length / PAGE_SIZE));
  const paginatedMemories = memories.slice(
    page * PAGE_SIZE,
    (page + 1) * PAGE_SIZE,
  );

  const handleDelete = async () => {
    if (!memoryToDelete) return;
    try {
      await api.delete(MEMORY_ENDPOINTS.BY_ID(memoryToDelete.id));
      toast({ title: "Memory deleted", variant: "success" });
      if (selectedMemory?.id === memoryToDelete.id) setSelectedMemory(null);
      setMemoryToDelete(null);
      void loadInitial();
    } catch (error) {
      toast({
        title: "Failed to delete memory",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    }
  };

  // 开关切换即重跑同一 query：两个开关直接对应既有请求参数，不是新造过滤。
  const toggleMechanismFilter = async (
    key: "observations" | "invalidated",
    next: boolean,
  ) => {
    const nextObservations =
      key === "observations" ? next : includeObservations;
    const nextInvalidated = key === "invalidated" ? next : includeInvalidated;
    if (key === "observations") setIncludeObservations(next);
    else setIncludeInvalidated(next);

    const q = query.trim();
    if (!q || !searchMode) return;
    setIsLoading(true);
    try {
      const rows = await fetchSearchPage(q, {
        includeObservations: nextObservations,
        includeInvalidated: nextInvalidated,
      });
      setMemories(rows);
      setPage(0);
    } catch (error) {
      toast({
        title: "Failed to re-run search",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  };

  const columns = [
    {
      key: "memory" as keyof Memory,
      label: "Content",
      width: 400,
      render: (value: string, row: Memory) => (
        <div className="flex items-center gap-2">
          <span className="line-clamp-2 text-sm">{value}</span>
          {isObservation(row) && <ObservationBadge />}
        </div>
      ),
    },
    ...(searchMode
      ? [
          {
            key: "score" as keyof Memory,
            label: "Score",
            width: 80,
            render: (value: number | undefined) =>
              typeof value === "number" ? value.toFixed(3) : "--",
          },
        ]
      : []),
    {
      key: "invalid_at" as keyof Memory,
      label: "Status",
      width: 100,
      render: (_value: unknown, row: Memory) => <ValidityBadge memory={row} />,
    },
    {
      key: "access_count" as keyof Memory,
      label: "Accesses",
      width: 90,
      align: "right" as const,
      render: (value: number | null | undefined) => formatCount(value),
    },
    { key: "user_id" as keyof Memory, label: "User", width: 100 },
    { key: "agent_id" as keyof Memory, label: "Agent", width: 100 },
    {
      key: "created_at" as keyof Memory,
      label: "Created",
      width: 120,
      render: (value: string) =>
        value ? format(new Date(value), "MMM d, yyyy") : "--",
    },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold font-fustat">Memories</h1>

      <div className="flex gap-3">
        <Input
          placeholder="Search memories (semantic)…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleSearchKeyDown}
          className="w-96"
        />
        <Button variant="outline" size="sm" onClick={loadInitial}>
          <Search className="size-3.5 mr-1" />
          {searchMode ? "Re-search" : "Search"}
        </Button>
        {searchMode && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setQuery("");
              void loadInitial();
            }}
          >
            Clear
          </Button>
        )}
      </div>

      {searchMode && !isLoading && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-6">
            <label className="flex items-center gap-2 text-xs text-onSurface-default-secondary">
              <Switch
                checked={includeObservations}
                onCheckedChange={(checked) =>
                  void toggleMechanismFilter("observations", checked)
                }
              />
              含观察（include_observations）
            </label>
            <label className="flex items-center gap-2 text-xs text-onSurface-default-secondary">
              <Switch
                checked={includeInvalidated}
                onCheckedChange={(checked) =>
                  void toggleMechanismFilter("invalidated", checked)
                }
              />
              含已失效（include_invalidated）
            </label>
          </div>
          {graphStatus && (
            <GraphStatusBar
              status={graphStatus}
              budgetSeconds={graphBudgetSeconds}
              graphKey={searchGraphKey}
            />
          )}
        </div>
      )}

      {isLoading ? (
        <TableSkeleton rows={5} columns={4} />
      ) : memories.length === 0 ? (
        <EmptyState
          title={searchMode ? "No matches" : "No memories yet"}
          description={
            searchMode
              ? "No memories matched your search query."
              : "Create your first memory by sending a POST /memories request."
          }
        >
          <pre className="text-xs text-left bg-surface-default-secondary p-3 rounded font-mono overflow-x-auto mt-3 max-w-lg">
            {`curl -X POST ${apiUrl}/memories \\\\
  -H "X-API-Key: *** \\\\
  -H "Content-Type: application/json" \\\\
  -d '{"messages": [{"role": "user", "content": "I like hiking"}], "user_id": "alice"}'`}
          </pre>
          <a
            href="https://docs.mem0.ai/open-source/features/rest-api#memory-operations"
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-onSurface-default-tertiary underline underline-offset-4 hover:text-onSurface-default-primary mt-2"
          >
            REST API reference
          </a>
        </EmptyState>
      ) : (
        <>
          <Card className="border-memBorder-primary overflow-hidden">
            <DataTable
              data={paginatedMemories}
              columns={columns}
              getRowKey={(row) => row.id}
              onRowClick={(row) => setSelectedMemory(row)}
              getRowClassName={(row) =>
                selectedMemory?.id === row.id
                  ? "bg-surface-default-tertiary"
                  : undefined
              }
            />
          </Card>
          {!searchMode && (
            <div className="flex items-center justify-between text-sm text-onSurface-default-tertiary">
              <span>
                {memories.length === 0
                  ? "0 memories"
                  : `${page * PAGE_SIZE + 1}–${Math.min(
                      (page + 1) * PAGE_SIZE,
                      memories.length,
                    )} of ${totalCount ?? `${memories.length}+`}`}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page === 0}
                  onClick={() => setPage((p) => p - 1)}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= totalPages - 1 && !nextCursor}
                  onClick={() => {
                    if (page < totalPages - 1) {
                      setPage((p) => p + 1);
                    } else if (nextCursor && !isLoadingMore) {
                      void loadMore();
                      setPage((p) => p + 1);
                    }
                  }}
                >
                  {isLoadingMore ? "Loading…" : "Next"}
                </Button>
              </div>
            </div>
          )}
        </>
      )}

      <MemoryDetailSheet
        memory={selectedMemory}
        open={!!selectedMemory}
        onOpenChange={(open) => {
          if (!open) setSelectedMemory(null);
        }}
        onSelectMemory={(row) => setSelectedMemory(row)}
        onDelete={(row) => setMemoryToDelete(row)}
        graphKey={searchMode ? searchGraphKey : null}
      />

      <DeleteConfirmationModal
        isOpen={!!memoryToDelete}
        onClose={() => setMemoryToDelete(null)}
        onConfirm={handleDelete}
        title="Delete memory"
        description="This memory will be permanently removed. This cannot be undone."
        itemName={memoryToDelete?.id ?? ""}
        confirmButtonText="Delete"
      />
    </div>
  );
}
