"use client";

import { useEffect, useState } from "react";
import { Trash2, Search } from "lucide-react";
import { format } from "date-fns";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/ui/card";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { MEMORY_ENDPOINTS } from "@/utils/api-endpoints";
import { Memory } from "@/types/api";

// Page size for cursor pagination. The backend returns { results, next_cursor } —
// we keep a page buffer and append further pages as the user navigates.
const PAGE_SIZE = 10;
const CURSOR_FETCH_LIMIT = 10;

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
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";

  // Browse mode: cursor-paginated GET /memories (newest first).
  const fetchBrowsePage = async (cursor: string | null) => {
    const params: Record<string, string | number> = {
      top_k: CURSOR_FETCH_LIMIT,
    };
    if (cursor) params.cursor = cursor;
    const res = await api.get(MEMORY_ENDPOINTS.BASE, { params });
    const raw = res.data?.results ?? res.data ?? [];
    const rows: Memory[] = Array.isArray(raw) ? raw : [];
    setNextCursor(res.data?.next_cursor ?? null);
    setHasMore(res.data?.has_more === true || !!res.data?.next_cursor);
    if (typeof res.data?.total === "number") setTotalCount(res.data.total);
    return rows;
  };

  // Search mode: semantic (vector) recall via POST /search over the whole collection.
  const fetchSearchPage = async (q: string) => {
    const res = await api.post(MEMORY_ENDPOINTS.SEARCH, {
      query: q,
      top_k: 50,
      filters: { user_id: "*" },
    });
    const raw = res.data?.results ?? res.data ?? [];
    return (Array.isArray(raw) ? raw : []) as Memory[];
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
      } else {
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

  const columns = [
    {
      key: "memory" as keyof Memory,
      label: "Content",
      width: 400,
      render: (value: string) => (
        <span className="line-clamp-2 text-sm">{value}</span>
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

      <Sheet
        open={!!selectedMemory}
        onOpenChange={(open) => {
          if (!open) setSelectedMemory(null);
        }}
      >
        <SheetContent className="sm:max-w-md">
          <SheetHeader>
            <SheetTitle>Memory Detail</SheetTitle>
            <SheetDescription className="sr-only">
              View memory content and metadata
            </SheetDescription>
          </SheetHeader>
          {selectedMemory && (
            <div className="mt-6 space-y-4">
              <div className="space-y-1">
                <Label className="text-xs text-onSurface-default-tertiary">
                  Content
                </Label>
                <p className="text-sm">{selectedMemory.memory}</p>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-1">
                  <Label className="text-xs text-onSurface-default-tertiary">
                    ID
                  </Label>
                  <p className="text-xs font-mono break-all">
                    {selectedMemory.id}
                  </p>
                </div>
                {selectedMemory.user_id && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      User
                    </Label>
                    <p className="text-sm">{selectedMemory.user_id}</p>
                  </div>
                )}
                {selectedMemory.agent_id && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      Agent
                    </Label>
                    <p className="text-sm">{selectedMemory.agent_id}</p>
                  </div>
                )}
                {selectedMemory.created_at && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      Created
                    </Label>
                    <p className="text-sm">
                      {new Date(selectedMemory.created_at).toLocaleString()}
                    </p>
                  </div>
                )}
              </div>
              <Button
                variant="outline"
                size="sm"
                className="text-onSurface-danger-primary"
                onClick={() => setMemoryToDelete(selectedMemory)}
              >
                <Trash2 className="size-3.5 mr-1" />
                Delete memory
              </Button>
            </div>
          )}
        </SheetContent>
      </Sheet>

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
