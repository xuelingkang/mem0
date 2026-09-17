"use client";

import { useState } from "react";
import { Download, FileJson, FileSpreadsheet, Loader2 } from "lucide-react";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/utils/api";
import { MEMORY_ENDPOINTS } from "@/utils/api-endpoints";
import { useApiQuery } from "@/hooks/use-api-query";
import { getErrorMessage } from "@/lib/error-message";

type ExportFormat = "json" | "csv";

const FORMATS: {
  id: ExportFormat;
  label: string;
  hint: string;
  icon: typeof FileJson;
}[] = [
  {
    id: "json",
    label: "JSON",
    hint: "Structured records with metadata included.",
    icon: FileJson,
  },
  {
    id: "csv",
    label: "CSV",
    hint: "Flat table, one row per memory.",
    icon: FileSpreadsheet,
  },
];

const buildStamp = () =>
  new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");

export default function ExportPage() {
  const [format, setFormat] = useState<ExportFormat>("json");
  const [isExporting, setIsExporting] = useState(false);

  const { data: total = 0 } = useApiQuery<number>(
    async () => {
      const res = await api.get<{ total?: number }>(MEMORY_ENDPOINTS.BASE, {
        params: { top_k: 1 },
      });
      return res.data?.total ?? 0;
    },
    { initialData: 0 },
  );

  const handleExport = async () => {
    setIsExporting(true);
    try {
      const res = await api.get<Blob>(MEMORY_ENDPOINTS.EXPORT, {
        params: { format },
        responseType: "blob",
      });

      const filename = `mem0-memories-${buildStamp()}.${format}`;
      const url = URL.createObjectURL(res.data);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);

      toast({
        title: "Export ready",
        description: `Downloaded ${filename}`,
      });
    } catch (err) {
      toast({
        title: "Export failed",
        description: getErrorMessage(err, "Could not export memories"),
        variant: "destructive",
      });
    } finally {
      setIsExporting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold font-fustat">Export</h1>
        <p className="text-sm text-onSurface-default-secondary mt-1">
          Download every memory in your instance as a single file.
        </p>
      </div>

      <Card className="border-memBorder-primary">
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">Format</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {FORMATS.map((option) => {
              const Icon = option.icon;
              const isActive = format === option.id;
              return (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => setFormat(option.id)}
                  className={`flex items-start gap-3 rounded-md border p-4 text-left transition-colors ${
                    isActive
                      ? "border-memPurple-300 bg-surface-default-brand"
                      : "border-memBorder-primary hover:bg-surface-default-secondary"
                  }`}
                >
                  <Icon className="size-4 mt-0.5 shrink-0 text-onSurface-default-secondary" />
                  <span>
                    <span className="block text-sm font-medium">
                      {option.label}
                    </span>
                    <span className="block text-xs text-onSurface-default-tertiary mt-0.5">
                      {option.hint}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-memBorder-primary pt-4">
            <p className="text-xs text-onSurface-default-tertiary">
              {total > 0
                ? `Exports the full collection — ${total.toLocaleString()} memories, not just the current page.`
                : "Exports the full collection, not just the current page."}
            </p>
            <Button onClick={() => void handleExport()} disabled={isExporting}>
              {isExporting ? (
                <Loader2 className="size-4 mr-2 animate-spin" />
              ) : (
                <Download className="size-4 mr-2" />
              )}
              {isExporting ? "Exporting..." : `Export ${format.toUpperCase()}`}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
