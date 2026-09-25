import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { cn } from "@/lib/utils";

/**
 * Top-bar indexer health indicator.
 *
 * Polls the indexer `GET /health` via the typed API client. Green on
 * success, red on error, grey while loading. Also surfaces the ADR-004 §3
 * index-schema stamp state when the indexer reports it: a re-index
 * requirement, an unverified (adoption pending) index, or an in-progress
 * rebuild with its done/total progress. An older indexer that predates
 * these fields (no `index_status`) falls back to the plain `status` dot.
 *
 * No live indexer is reachable in CI/build, so the dot shows loading/down
 * there — that is expected.
 */
type DotColor = "ok" | "degraded" | "down" | "unknown";

const DOT_COLOR: Record<DotColor, string> = {
  ok: "bg-success",
  degraded: "bg-warning",
  down: "bg-danger",
  unknown: "bg-muted-foreground",
};

/** ADR-004 §3 index status, when the indexer reports one. */
type IndexStatus = "ok" | "unverified" | "reindex_required" | "rebuilding";

/** Shape of the indexer `/health` response. */
interface HealthResponse {
  status?: string;
  index_status?: IndexStatus;
  reindex_reason?: string;
  rebuild_progress?: { done: number; total: number };
}

interface HealthDisplay {
  dot: DotColor;
  label: string;
  /** Extra detail for a tooltip (e.g. the mismatch reason). */
  title?: string;
}

/** Maps the query state to a dot color and label. Pure for testability. */
export function healthStatus(state: {
  isLoading: boolean;
  isError: boolean;
  data?: HealthResponse;
}): HealthDisplay {
  if (state.isLoading) return { dot: "unknown", label: "unknown" };
  if (state.isError || !state.data) return { dot: "down", label: "down" };

  const data = state.data;
  const indexStatus = data.index_status;

  if (indexStatus === "reindex_required") {
    return { dot: "degraded", label: "Re-index required", title: data.reindex_reason };
  }
  if (indexStatus === "unverified") {
    return { dot: "degraded", label: "Index unverified", title: data.reindex_reason };
  }
  if (indexStatus === "rebuilding") {
    const progress = data.rebuild_progress;
    const label = progress ? `Rebuilding ${progress.done}/${progress.total}` : "Rebuilding";
    return { dot: "degraded", label };
  }
  if (indexStatus === "ok" || indexStatus === undefined) {
    // No index_status (an indexer older than 1.1.0) falls back to `status`.
    if (data.status && data.status !== "ok") return { dot: "degraded", label: data.status };
    return { dot: "ok", label: "ok" };
  }
  return { dot: "ok", label: "ok" };
}

export function HealthIndicator() {
  const query = useQuery({
    queryKey: ["health"],
    queryFn: () => api.get<HealthResponse>("/health"),
    refetchInterval: 30_000,
  });

  const display = healthStatus(query);

  return (
    <div className="flex items-center gap-2" aria-live="polite" title={display.title}>
      <span
        className={cn("h-2 w-2 rounded-full", DOT_COLOR[display.dot])}
        aria-hidden="true"
      />
      <span className="text-[13px] text-muted-foreground">
        indexer: {display.label}
      </span>
    </div>
  );
}
