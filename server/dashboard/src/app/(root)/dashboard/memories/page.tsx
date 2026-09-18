"use client";

import { useEffect, useState } from "react";
import { Trash2, Search } from "lucide-react";
import { format } from "date-fns";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import {
  ENTITY_ENDPOINTS,
  GRAPH_ENDPOINTS,
  MEMORY_ENDPOINTS,
} from "@/utils/api-endpoints";
import {
  Entity,
  GraphStats,
  GraphStatus,
  Memory,
  MemoryListResponse,
} from "@/types/api";
import { formatCount, formatScore, isObservation } from "@/utils/mechanism";
import { deriveGraphKey } from "@/utils/graph-key";
import {
  hasOwnerFilter,
  matchesOwnerFilter,
  OwnerFilter,
} from "@/utils/owner-filter";
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
// 归属者过滤里「不限 agent」的哨兵值：Radix Select 不接受空串作为 SelectItem 的值。
const ALL_AGENTS = "__all__";
// 过滤态走游标时的取数批量：展示页大小（PAGE_SIZE）与取数批量是两件事，
// 按集合单页上限取（服务端 ALL_MEMORIES_LIMIT = 1000）。
const FILTER_WALK_PAGE_LIMIT = 1000;
// 过滤态下把游标走完的防御性上限。按实际页数收敛（本机 4153 行 ⇒ 6 次请求，末次为空页），
// 触顶后退回「边翻页边加载」，只影响总数是否已精确，不影响过滤与翻页本身的正确性。
const MAX_FILTER_WALK_PAGES = 20;

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
  // 归属者过滤：作用于清单（浏览模式）。`user_id` 沿用上游输入框形态（Enter 才生效），
  // `agent_id` 的候选值取自后端 `/entities` 的真实数据，不硬编码 agent id。
  const [userFilterInput, setUserFilterInput] = useState("");
  const [ownerFilter, setOwnerFilter] = useState<OwnerFilter>({});
  const [agentOptions, setAgentOptions] = useState<Entity[]>([]);
  // 过滤态的游标走完过程（见 loadListing 的说明）。
  const [isFilterWalking, setIsFilterWalking] = useState(false);
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
  const fetchBrowsePage = async (
    cursor: string | null,
    limit: number = CURSOR_FETCH_LIMIT,
  ) => {
    const params: Record<string, string | number> = {
      top_k: limit,
    };
    if (cursor) params.cursor = cursor;
    const res = await api.get<MemoryListResponse>(MEMORY_ENDPOINTS.BASE, {
      params,
    });
    const raw = res.data?.results ?? res.data ?? [];
    const rows: Memory[] = Array.isArray(raw) ? raw : [];
    const next = res.data?.next_cursor ?? null;
    setNextCursor(next);
    setHasMore(res.data?.has_more === true || !!next);
    if (typeof res.data?.total === "number") setTotalCount(res.data.total);
    return { rows, nextCursor: next };
  };

  /**
   * 取浏览清单。无过滤时取首页（与既有行为逐位一致）；过滤态下继续把同一游标走完。
   *
   * 为什么过滤不在服务端做：`GET /memories` 的按作用域路径（`?user_id=&agent_id=`）
   * 不返回 `next_cursor` / `total`，单页上限 1000 行——本机 ying 一个 agent 就有 3260 条，
   * 直接用它会让主维度被截断，并丢掉既有游标翻页；而管理面列表（无作用域）本就是既有
   * 游标通道，其排序口径与行序列化器与页面完全一致。所以归属者过滤落在这条既有通道的
   * 行集合上（`matchesOwnerFilter`），过滤态把游标走完，换来精确的匹配条数与无遗漏翻页。
   */
  const loadListing = async (filter: OwnerFilter) => {
    const filterActive = hasOwnerFilter(filter);
    const limit = filterActive ? FILTER_WALK_PAGE_LIMIT : CURSOR_FETCH_LIMIT;
    setMemories([]);
    const first = await fetchBrowsePage(null, limit);
    setMemories(first.rows);
    if (!filterActive) return;
    let cursor = first.nextCursor;
    setIsFilterWalking(true);
    try {
      for (let hop = 0; cursor && hop < MAX_FILTER_WALK_PAGES; hop++) {
        const next = await fetchBrowsePage(cursor, limit);
        cursor = next.nextCursor;
        if (next.rows.length > 0) {
          setMemories((prev: Memory[]) => [...prev, ...next.rows]);
        }
      }
    } finally {
      setIsFilterWalking(false);
    }
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
        await loadListing(ownerFilter);
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
      setMemories((prev: Memory[]) => [...prev, ...more.rows]);
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

  // agent_id 的候选值来自后端真实数据：`/entities` 的 agent 桶（服务端按全集合统计），
  // 不硬编码 agent id。取不到时只保留「不限」选项，不影响清单本身可用。
  useEffect(() => {
    const loadAgentOptions = async () => {
      try {
        const res = await api.get<Entity[]>(ENTITY_ENDPOINTS.BASE);
        setAgentOptions(
          (res.data ?? []).filter((entity) => entity.type === "agent"),
        );
      } catch (error) {
        setAgentOptions([]);
        toast({
          title: "Failed to load agent options",
          description: getErrorMessage(error),
          variant: "destructive",
        });
      }
    };
    void loadAgentOptions();
  }, []);

  const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") void loadInitial();
  };

  /**
   * 应用归属者过滤：清掉选中的行、回到首页，并按新条件重取清单。
   *
   * 过滤条件只在用户显式提交时改变（`user_id` 输入框按 Enter、`agent_id` 下拉选中即生效），
   * 因此翻页过程不会重置它——`loadMore` / `handleDelete` 都只重取数据，不碰过滤状态。
   */
  const applyOwnerFilter = async (next: OwnerFilter) => {
    setOwnerFilter(next);
    setPage(0);
    setSelectedMemory(null);
    setIsLoading(true);
    try {
      await loadListing(next);
    } catch (error) {
      toast({
        title: "Failed to apply the owner filter",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  };

  // 归属者过滤只作用于清单（浏览模式）：检索模式是通配作用域下的语义召回，
  // 是否限定归属者是另一件事，本批不改检索路径的语义。
  const ownerFilterActive = !searchMode && hasOwnerFilter(ownerFilter);
  const visibleMemories = ownerFilterActive
    ? memories.filter((row) => matchesOwnerFilter(row, ownerFilter))
    : memories;
  const totalPages = Math.max(1, Math.ceil(visibleMemories.length / PAGE_SIZE));
  const paginatedMemories = visibleMemories.slice(
    page * PAGE_SIZE,
    (page + 1) * PAGE_SIZE,
  );
  // 过滤态的「of N」是已加载的匹配行数：游标走完即精确，后缀 `+` 表示还有未加载的页。
  const visibleTotal = ownerFilterActive
    ? `${visibleMemories.length}${nextCursor ? "+" : ""}`
    : `${totalCount ?? `${memories.length}+`}`;

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

      {!searchMode && (
        <div className="flex flex-wrap items-center gap-3">
          <Input
            placeholder="Filter by User ID (optional)"
            value={userFilterInput}
            onChange={(e) => setUserFilterInput(e.target.value)}
            onKeyDown={(e) => {
              // 与上游一致：输入框在 Enter 时提交（不是每次击键都重取清单）。
              if (e.key === "Enter") {
                void applyOwnerFilter({
                  ...ownerFilter,
                  user_id: userFilterInput,
                });
              }
            }}
            className="w-64"
          />
          <Select
            value={ownerFilter.agent_id ?? ALL_AGENTS}
            onValueChange={(value) =>
              void applyOwnerFilter({
                ...ownerFilter,
                agent_id: value === ALL_AGENTS ? undefined : value,
              })
            }
          >
            <SelectTrigger className="w-56">
              <SelectValue placeholder="Filter by Agent ID (optional)" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_AGENTS}>All agents</SelectItem>
              {agentOptions.map((agent) => (
                <SelectItem key={agent.id} value={agent.id}>
                  {/* 同一来源给出该 agent 的条数，便于与清单计数核对。 */}
                  {`${agent.id} · ${formatCount(agent.total_memories)}`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {hasOwnerFilter(ownerFilter) && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setUserFilterInput("");
                void applyOwnerFilter({});
              }}
            >
              Reset
            </Button>
          )}
        </div>
      )}

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

      {isLoading || (isFilterWalking && visibleMemories.length === 0) ? (
        <TableSkeleton rows={5} columns={4} />
      ) : visibleMemories.length === 0 ? (
        ownerFilterActive ? (
          <EmptyState
            title="No memories match this filter"
            description="No memory carries the selected user_id / agent_id. Clear the owner filter to see the whole listing."
          />
        ) : (
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
        )
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
                {visibleMemories.length === 0
                  ? "0 memories"
                  : `${page * PAGE_SIZE + 1}–${Math.min(
                      (page + 1) * PAGE_SIZE,
                      visibleMemories.length,
                    )} of ${visibleTotal}`}
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
                  disabled={
                    isFilterWalking || (page >= totalPages - 1 && !nextCursor)
                  }
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
