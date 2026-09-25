import { describe, expect, it } from "vitest";

import { healthStatus } from "@/components/layout/HealthIndicator";

function state(data?: Record<string, unknown>) {
  return { isLoading: false, isError: false, data };
}

describe("healthStatus", () => {
  it("ok index_status is healthy", () => {
    const result = healthStatus(state({ status: "ok", index_status: "ok" }));
    expect(result.dot).toBe("ok");
    expect(result.label).toBe("ok");
  });

  it("reindex_required is an error dot with the reason as the title", () => {
    const result = healthStatus(
      state({
        status: "ok",
        index_status: "reindex_required",
        reindex_reason: "vector store (milvus): embedding_model is A, configured B",
      })
    );
    expect(result.dot).toBe("degraded");
    expect(result.label).toBe("Re-index required");
    expect(result.title).toBe("vector store (milvus): embedding_model is A, configured B");
  });

  it("unverified is a warning dot labeled Index unverified", () => {
    const result = healthStatus(
      state({ status: "ok", index_status: "unverified", reindex_reason: "not yet verified" })
    );
    expect(result.dot).toBe("degraded");
    expect(result.label).toBe("Index unverified");
  });

  it("rebuilding shows done/total progress", () => {
    const result = healthStatus(
      state({
        status: "ok",
        index_status: "rebuilding",
        rebuild_progress: { done: 3, total: 10 },
      })
    );
    expect(result.label).toBe("Rebuilding 3/10");
  });

  it("rebuilding with no progress falls back to a plain label", () => {
    const result = healthStatus(state({ status: "ok", index_status: "rebuilding" }));
    expect(result.label).toBe("Rebuilding");
  });

  it("a missing index_status (older indexer) falls back to today's behaviour: status ok", () => {
    const result = healthStatus(state({ status: "ok" }));
    expect(result.dot).toBe("ok");
    expect(result.label).toBe("ok");
  });

  it("a missing index_status with a non-ok status is degraded", () => {
    const result = healthStatus(state({ status: "database unavailable" }));
    expect(result.dot).toBe("degraded");
    expect(result.label).toBe("database unavailable");
  });

  it("loading is unknown", () => {
    const result = healthStatus({ isLoading: true, isError: false, data: undefined });
    expect(result.dot).toBe("unknown");
  });

  it("error with no data is down", () => {
    const result = healthStatus({ isLoading: false, isError: true, data: undefined });
    expect(result.dot).toBe("down");
  });
});
