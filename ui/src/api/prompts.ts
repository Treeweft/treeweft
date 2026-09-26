import { ApiError, api } from "@/api/client";

/**
 * Prompt versions and pins (ADR-003). Admin-only.
 *
 * - `GET    /prompt-versions` → { operations, sources }
 * - `PUT    /prompt-pins/{operation}?dry_run=` → PinResult (deployment pin)
 * - `PUT    /prompt-pins/chunk_summary/sources/{source_id}?dry_run=` → PinResult (override)
 * - `DELETE /prompt-pins/chunk_summary/sources/{source_id}?dry_run=` → PinResult (clear override)
 * - `POST   /sources/{source_id}/resummarize` → ResummarizeResult (manual refresh)
 */

/** One registered prompt version, with its release notes. */
export interface PromptVersionInfo {
  version: number;
  notes: string;
}

/** A stored pin or override: the version plus who set it and when. */
export interface PinInfo {
  version: number;
  updated_at: string;
  updated_by: string | null;
}

/** Per-operation version registry, from `GET /prompt-versions`. */
export interface OperationVersions {
  versions: PromptVersionInfo[];
  latest: number;
  deployment_pin: PinInfo;
}

/** One row of the `sources` list in `GET /prompt-versions`. */
export interface PromptSourceRow {
  source_id: string;
  label: string;
  chunk_count: number;
  summary_prompt_version: number | null;
  summary_refresh_target: number | null;
  override: PinInfo | null;
  effective_version: number;
  stale: boolean;
  active_job_id: string | null;
}

/** The `GET /prompt-versions` response. */
export interface PromptVersionsResponse {
  operations: Record<string, OperationVersions>;
  sources: PromptSourceRow[];
}

/** One entry in a `PinResult`'s `enqueued` / `deferred` / `not_enqueued` lists. */
export interface PinResultItem {
  source_id: string;
  job_id?: string | null;
  blocking_job_id?: string;
  reason?: string;
  /** `null` when the source's recorded chunk_summary version is unknown. */
  current_version: number | null;
  target_version: number;
  chunk_count: number;
}

/**
 * The shared response shape for every pin/override mutation (dry run and
 * real), and for the confirmation panel rendered from it.
 */
export interface PinResult {
  dry_run: boolean;
  operation: string;
  scope: string;
  /** `null` for a new override, or a source with an unknown recorded version. */
  previous_version: number | null;
  version: number;
  enqueued: PinResultItem[];
  deferred: PinResultItem[];
  not_enqueued: PinResultItem[];
  group_id: string | null;
  total_chunks: number;
  effect: string | null;
}

/** The `POST /sources/{id}/resummarize` response. */
export interface ResummarizeResult {
  job_id: string | null;
  target_version?: number;
  reason?: string;
  deferred?: boolean;
}

// --------------------------------------------------------------------------
// Fetch functions
// --------------------------------------------------------------------------

export function getPromptVersions(): Promise<PromptVersionsResponse> {
  return api.get<PromptVersionsResponse>("/prompt-versions");
}

/** Set (or dry-run) the deployment-wide pin for `operation`. */
export function setDeploymentPin(
  operation: string,
  version: number,
  dryRun: boolean,
): Promise<PinResult> {
  return api.put<PinResult>(
    `/prompt-pins/${encodeURIComponent(operation)}?dry_run=${dryRun}`,
    { version },
  );
}

/** Set (or dry-run) a per-source `chunk_summary` override. */
export function setOverride(
  sourceId: string,
  version: number,
  dryRun: boolean,
): Promise<PinResult> {
  return api.put<PinResult>(
    `/prompt-pins/chunk_summary/sources/${encodeURIComponent(sourceId)}?dry_run=${dryRun}`,
    { version },
  );
}

/** Clear (or dry-run clearing) a per-source `chunk_summary` override. */
export function clearOverride(
  sourceId: string,
  dryRun: boolean,
): Promise<PinResult> {
  return api.del<PinResult>(
    `/prompt-pins/chunk_summary/sources/${encodeURIComponent(sourceId)}?dry_run=${dryRun}`,
  );
}

/** Trigger a manual refresh for one source. */
export function resummarize(sourceId: string): Promise<ResummarizeResult> {
  return api.post<ResummarizeResult>(
    `/sources/${encodeURIComponent(sourceId)}/resummarize`,
  );
}

// --------------------------------------------------------------------------
// Pure helpers (unit-tested)
// --------------------------------------------------------------------------

/** Count sources flagged stale. */
export function staleCount(sources: PromptSourceRow[]): number {
  return sources.filter((s) => s.stale).length;
}

/** Format a chunk count with thousands separators. */
export function formatChunks(n: number): string {
  return n.toLocaleString();
}

/**
 * Render a prompt version, or "—" for `null` -- a new override with no
 * prior version, or a source whose recorded chunk_summary version is
 * unknown.
 */
export function formatVersion(v: number | null): string {
  return v === null ? "—" : String(v);
}

/**
 * Read `valid_versions` off an `ApiError`'s body (the 400 shape from
 * `PUT/DELETE /prompt-pins/...`), sorted ascending. Returns `[]` for
 * anything else, so callers can render it unconditionally.
 */
export function validVersionsFromError(err: unknown): number[] {
  if (!(err instanceof ApiError)) return [];
  const body = err.body;
  if (!body || typeof body !== "object") return [];
  const raw = (body as { valid_versions?: unknown }).valid_versions;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((v): v is number => typeof v === "number")
    .slice()
    .sort((a, b) => a - b);
}
