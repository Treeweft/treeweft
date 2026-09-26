import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";
import {
  formatChunks,
  staleCount,
  validVersionsFromError,
  type PromptSourceRow,
} from "@/api/prompts";

function source(over: Partial<PromptSourceRow>): PromptSourceRow {
  return {
    source_id: "src_1",
    label: "org/repo",
    chunk_count: 100,
    summary_prompt_version: 3,
    summary_refresh_target: null,
    override: null,
    effective_version: 3,
    stale: false,
    active_job_id: null,
    ...over,
  };
}

describe("staleCount", () => {
  it("counts only stale sources", () => {
    const sources = [
      source({ source_id: "a", stale: true }),
      source({ source_id: "b", stale: false }),
      source({ source_id: "c", stale: true }),
    ];
    expect(staleCount(sources)).toBe(2);
  });

  it("returns 0 for an empty list", () => {
    expect(staleCount([])).toBe(0);
  });
});

describe("formatChunks", () => {
  it("adds thousands separators", () => {
    expect(formatChunks(19186)).toBe("19,186");
  });

  it("leaves small numbers unchanged", () => {
    expect(formatChunks(42)).toBe("42");
  });

  it("formats zero", () => {
    expect(formatChunks(0)).toBe("0");
  });
});

describe("validVersionsFromError", () => {
  it("reads valid_versions off an ApiError body", () => {
    const err = new ApiError(400, "unknown chunk_summary version 7", {
      detail: "unknown chunk_summary version 7",
      valid_versions: [4, 3],
    });
    expect(validVersionsFromError(err)).toEqual([3, 4]);
  });

  it("returns [] when the body has no valid_versions", () => {
    const err = new ApiError(400, "bad request", { detail: "bad request" });
    expect(validVersionsFromError(err)).toEqual([]);
  });

  it("returns [] for a non-ApiError", () => {
    expect(validVersionsFromError(new Error("boom"))).toEqual([]);
  });

  it("returns [] when the body is missing entirely", () => {
    expect(validVersionsFromError(new ApiError(400, "bad request"))).toEqual(
      [],
    );
  });
});
