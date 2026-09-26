import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import type { PinResult, PromptVersionsResponse } from "@/api/prompts";

const getMock = vi.fn();
const putMock = vi.fn();
const delMock = vi.fn();
const postMock = vi.fn();
vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>(
    "@/api/client",
  );
  return {
    ...actual,
    api: {
      get: (p: string) => getMock(p),
      put: (p: string, b?: unknown) => putMock(p, b),
      del: (p: string) => delMock(p),
      post: (p: string, b?: unknown) => postMock(p, b),
    },
  };
});

function testClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

async function renderPrompts() {
  const { PromptsPage } = await import("@/pages/PromptsPage");
  return render(
    <QueryClientProvider client={testClient()}>
      <PromptsPage />
    </QueryClientProvider>,
  );
}

function versionsResponse(): PromptVersionsResponse {
  return {
    operations: {
      chunk_summary: {
        versions: [
          { version: 3, notes: "Nonce-fenced one-sentence summary" },
          { version: 4, notes: "Shorter summary" },
        ],
        latest: 4,
        deployment_pin: {
          version: 3,
          updated_at: "2026-09-25T12:00:00Z",
          updated_by: null,
        },
      },
      hyde: {
        versions: [{ version: 1, notes: "Hypothetical snippet" }],
        latest: 1,
        deployment_pin: {
          version: 1,
          updated_at: "2026-09-25T12:00:00Z",
          updated_by: null,
        },
      },
    },
    sources: [
      {
        source_id: "src_1",
        label: "org/repo",
        chunk_count: 18234,
        summary_prompt_version: 3,
        summary_refresh_target: null,
        override: null,
        effective_version: 3,
        stale: false,
        active_job_id: null,
      },
      {
        source_id: "src_2",
        label: "org/other",
        chunk_count: 912,
        summary_prompt_version: 2,
        summary_refresh_target: null,
        override: { version: 2, updated_at: "2026-09-25T12:00:00Z", updated_by: "user_1" },
        effective_version: 2,
        stale: true,
        active_job_id: null,
      },
    ],
  };
}

function dryRunPinResult(): PinResult {
  return {
    dry_run: true,
    operation: "chunk_summary",
    scope: "deployment",
    previous_version: 3,
    version: 4,
    enqueued: [
      {
        source_id: "src_1",
        current_version: 3,
        target_version: 4,
        chunk_count: 18234,
      },
    ],
    deferred: [
      {
        source_id: "src_2",
        blocking_job_id: "job_blocking",
        current_version: 2,
        target_version: 4,
        chunk_count: 912,
      },
    ],
    not_enqueued: [
      {
        source_id: "src_3",
        reason: "index is reindex_required: rebuild in progress",
        current_version: 2,
        target_version: 4,
        chunk_count: 40,
      },
    ],
    group_id: null,
    total_chunks: 19186,
    effect: null,
  };
}

function realPinResult(): PinResult {
  return {
    ...dryRunPinResult(),
    dry_run: false,
    enqueued: [
      {
        source_id: "src_1",
        job_id: "job_1",
        current_version: 3,
        target_version: 4,
        chunk_count: 18234,
      },
    ],
    group_id: "grp_1",
  };
}

afterEach(() => {
  cleanup();
  getMock.mockReset();
  putMock.mockReset();
  delMock.mockReset();
  postMock.mockReset();
});

describe("PromptsPage", () => {
  it("renders operation versions, notes, pins and per-source rows", async () => {
    getMock.mockResolvedValue(versionsResponse());
    await renderPrompts();

    expect(await screen.findByText("chunk_summary")).toBeTruthy();
    expect(screen.getByText("hyde")).toBeTruthy();
    expect(
      screen.getByText("Nonce-fenced one-sentence summary"),
    ).toBeTruthy();
    expect(screen.getByText("Hypothetical snippet")).toBeTruthy();

    const sourcesTable = screen.getByRole("table", { name: /sources/i });
    const row1 = within(sourcesTable).getByText("org/repo").closest("tr")!;
    expect(within(row1).getAllByText("3").length).toBeGreaterThan(0); // built + target version

    const row2 = within(sourcesTable).getByText("org/other").closest("tr")!;
    expect(within(row2).getByText(/stale/i)).toBeTruthy();
  });

  it("issues a dry-run PUT before showing the deployment-pin confirmation", async () => {
    getMock.mockResolvedValue(versionsResponse());
    putMock.mockResolvedValueOnce(dryRunPinResult());
    await renderPrompts();
    await screen.findByText("chunk_summary");

    fireEvent.change(screen.getByLabelText(/chunk_summary deployment pin/i), {
      target: { value: "4" },
    });

    const panel = await screen.findByRole("region", {
      name: /confirm prompt change/i,
    });
    expect(putMock).toHaveBeenCalledWith(
      "/prompt-pins/chunk_summary?dry_run=true",
      { version: 4 },
    );
    expect(putMock).toHaveBeenCalledTimes(1);

    // enqueued / deferred / not-enqueued sources with chunk counts and total
    expect(within(panel).getByText(/src_1/)).toBeTruthy();
    expect(within(panel).getByText(/18,234/)).toBeTruthy();
    expect(within(panel).getByText(/src_2/)).toBeTruthy();
    expect(within(panel).getByText(/job_blocking/)).toBeTruthy();
    expect(within(panel).getByText(/src_3/)).toBeTruthy();
    expect(within(panel).getByText(/reindex_required/)).toBeTruthy();
    expect(within(panel).getByText(/19,186/)).toBeTruthy();
  });

  it("renders — for a null previous_version and a null current_version", async () => {
    getMock.mockResolvedValue(versionsResponse());
    const unknownVersionResult: PinResult = {
      ...dryRunPinResult(),
      previous_version: null,
      enqueued: [
        {
          source_id: "src_1",
          current_version: null,
          target_version: 4,
          chunk_count: 18234,
        },
      ],
      deferred: [],
      not_enqueued: [],
    };
    putMock.mockResolvedValueOnce(unknownVersionResult);
    await renderPrompts();
    await screen.findByText("chunk_summary");

    fireEvent.change(screen.getByLabelText(/chunk_summary deployment pin/i), {
      target: { value: "4" },
    });

    const panel = await screen.findByRole("region", {
      name: /confirm prompt change/i,
    });
    expect(within(panel).getByText(/chunk_summary: v— → v4/)).toBeTruthy();
    expect(within(panel).getByText(/src_1: v— → v4/)).toBeTruthy();
  });

  it("cancel sends no further request", async () => {
    getMock.mockResolvedValue(versionsResponse());
    putMock.mockResolvedValueOnce(dryRunPinResult());
    await renderPrompts();
    await screen.findByText("chunk_summary");

    fireEvent.change(screen.getByLabelText(/chunk_summary deployment pin/i), {
      target: { value: "4" },
    });
    await screen.findByRole("button", { name: /^cancel$/i });

    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));

    expect(putMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /^confirm$/i })).toBeNull();
  });

  it("confirm sends the real PUT and shows the resulting job id", async () => {
    getMock.mockResolvedValue(versionsResponse());
    putMock
      .mockResolvedValueOnce(dryRunPinResult())
      .mockResolvedValueOnce(realPinResult());
    await renderPrompts();
    await screen.findByText("chunk_summary");

    fireEvent.change(screen.getByLabelText(/chunk_summary deployment pin/i), {
      target: { value: "4" },
    });
    await screen.findByRole("button", { name: /^confirm$/i });
    fireEvent.click(screen.getByRole("button", { name: /^confirm$/i }));

    await screen.findByText(/job_1/);
    expect(putMock).toHaveBeenNthCalledWith(
      2,
      "/prompt-pins/chunk_summary?dry_run=false",
      { version: 4 },
    );
  });

  it("sets a per-source override via dry run then confirm", async () => {
    getMock.mockResolvedValue(versionsResponse());
    const overrideDryRun: PinResult = {
      ...dryRunPinResult(),
      scope: "src_1",
      enqueued: [
        {
          source_id: "src_1",
          current_version: 3,
          target_version: 4,
          chunk_count: 18234,
        },
      ],
      deferred: [],
      not_enqueued: [],
      total_chunks: 18234,
    };
    const overrideReal: PinResult = {
      ...overrideDryRun,
      dry_run: false,
      enqueued: [
        {
          source_id: "src_1",
          job_id: "job_override",
          current_version: 3,
          target_version: 4,
          chunk_count: 18234,
        },
      ],
    };
    putMock
      .mockResolvedValueOnce(overrideDryRun)
      .mockResolvedValueOnce(overrideReal);
    await renderPrompts();
    const sourcesTable = await screen.findByRole("table", { name: /sources/i });
    const row1 = within(sourcesTable).getByText("org/repo").closest("tr")!;

    fireEvent.change(within(row1).getByLabelText(/override/i), {
      target: { value: "4" },
    });

    await screen.findByRole("button", { name: /^confirm$/i });
    expect(putMock).toHaveBeenCalledWith(
      "/prompt-pins/chunk_summary/sources/src_1?dry_run=true",
      { version: 4 },
    );

    fireEvent.click(screen.getByRole("button", { name: /^confirm$/i }));
    await screen.findByText(/job_override/);
    expect(putMock).toHaveBeenNthCalledWith(
      2,
      "/prompt-pins/chunk_summary/sources/src_1?dry_run=false",
      { version: 4 },
    );
  });

  it("clears a per-source override via dry run then confirm", async () => {
    getMock.mockResolvedValue(versionsResponse());
    const clearDryRun: PinResult = {
      ...dryRunPinResult(),
      scope: "src_2",
      previous_version: 2,
      version: 3,
      enqueued: [
        {
          source_id: "src_2",
          current_version: 2,
          target_version: 3,
          chunk_count: 912,
        },
      ],
      deferred: [],
      not_enqueued: [],
      total_chunks: 912,
    };
    const clearReal: PinResult = {
      ...clearDryRun,
      dry_run: false,
      enqueued: [
        {
          source_id: "src_2",
          job_id: "job_clear",
          current_version: 2,
          target_version: 3,
          chunk_count: 912,
        },
      ],
    };
    delMock
      .mockResolvedValueOnce(clearDryRun)
      .mockResolvedValueOnce(clearReal);
    await renderPrompts();
    const sourcesTable = await screen.findByRole("table", { name: /sources/i });
    const row2 = within(sourcesTable).getByText("org/other").closest("tr")!;

    fireEvent.click(within(row2).getByRole("button", { name: /clear/i }));

    await screen.findByRole("button", { name: /^confirm$/i });
    expect(delMock).toHaveBeenCalledWith(
      "/prompt-pins/chunk_summary/sources/src_2?dry_run=true",
    );

    fireEvent.click(screen.getByRole("button", { name: /^confirm$/i }));
    await screen.findByText(/job_clear/);
    expect(delMock).toHaveBeenNthCalledWith(
      2,
      "/prompt-pins/chunk_summary/sources/src_2?dry_run=false",
    );
  });

  it("the refresh action calls POST /sources/{id}/resummarize", async () => {
    getMock.mockResolvedValue(versionsResponse());
    postMock.mockResolvedValue({ job_id: "job_refresh", target_version: 4 });
    await renderPrompts();
    const sourcesTable = await screen.findByRole("table", { name: /sources/i });
    const row1 = within(sourcesTable).getByText("org/repo").closest("tr")!;

    fireEvent.click(within(row1).getByRole("button", { name: /refresh/i }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith(
        "/sources/src_1/resummarize",
        undefined,
      ),
    );
  });

  it("shows an admin-required message on 403", async () => {
    getMock.mockRejectedValue(new ApiError(403, "forbidden"));
    await renderPrompts();
    expect(await screen.findByText(/admin access required/i)).toBeTruthy();
  });

  it("shows an admin-required message on 401", async () => {
    getMock.mockRejectedValue(new ApiError(401, "unauthorized"));
    await renderPrompts();
    expect(await screen.findByText(/admin access required/i)).toBeTruthy();
  });

  it("shows the valid versions on a 400 from the dry run", async () => {
    getMock.mockResolvedValue(versionsResponse());
    putMock.mockRejectedValueOnce(
      new ApiError(400, "unknown chunk_summary version 7", {
        detail: "unknown chunk_summary version 7",
        valid_versions: [3, 4],
      }),
    );
    await renderPrompts();
    await screen.findByText("chunk_summary");

    fireEvent.change(screen.getByLabelText(/chunk_summary deployment pin/i), {
      target: { value: "4" },
    });

    expect(await screen.findByText(/valid versions/i)).toBeTruthy();
    expect(screen.getByText(/3, 4/)).toBeTruthy();
  });
});
