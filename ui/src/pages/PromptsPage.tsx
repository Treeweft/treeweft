import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import {
  clearOverride,
  formatChunks,
  getPromptVersions,
  resummarize,
  setDeploymentPin,
  setOverride,
  staleCount,
  validVersionsFromError,
  type OperationVersions,
  type PinResult,
  type PinResultItem,
  type PromptSourceRow,
  type PromptVersionsResponse,
} from "@/api/prompts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";

const QK = ["prompt-versions"] as const;

/** Which action a pending confirmation panel is for. */
type PendingKind = "deployment" | "override-set" | "override-clear";

interface PendingAction {
  kind: PendingKind;
  operation: string;
  sourceId: string | null;
  result: PinResult;
}

export function PromptsPage() {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: QK,
    queryFn: getPromptVersions,
    retry: false,
    staleTime: 15_000,
  });

  const [pending, setPending] = useState<PendingAction | null>(null);
  const [lastResult, setLastResult] = useState<PinResult | null>(null);
  const [refreshResult, setRefreshResult] = useState<{
    sourceId: string;
    jobId: string | null;
    reason?: string;
  } | null>(null);

  const dryRun = useMutation({
    mutationFn: async (vars: {
      kind: PendingKind;
      operation: string;
      sourceId: string | null;
      version: number;
    }) => {
      if (vars.kind === "deployment") {
        return setDeploymentPin(vars.operation, vars.version, true);
      }
      if (vars.kind === "override-set") {
        return setOverride(vars.sourceId!, vars.version, true);
      }
      return clearOverride(vars.sourceId!, true);
    },
    onSuccess: (result, vars) => {
      setLastResult(null);
      setPending({
        kind: vars.kind,
        operation: vars.operation,
        sourceId: vars.sourceId,
        result,
      });
    },
  });

  const confirm = useMutation({
    mutationFn: async () => {
      if (!pending) throw new Error("no pending action");
      if (pending.kind === "deployment") {
        return setDeploymentPin(pending.operation, pending.result.version, false);
      }
      if (pending.kind === "override-set") {
        return setOverride(pending.sourceId!, pending.result.version, false);
      }
      return clearOverride(pending.sourceId!, false);
    },
    onSuccess: (result) => {
      setLastResult(result);
      setPending(null);
      void qc.invalidateQueries({ queryKey: QK });
    },
  });

  const refresh = useMutation({
    mutationFn: (sourceId: string) => resummarize(sourceId),
    onSuccess: (result, sourceId) => {
      setRefreshResult({
        sourceId,
        jobId: result.job_id,
        reason: result.reason,
      });
      void qc.invalidateQueries({ queryKey: QK });
    },
  });

  function startDeploymentPin(operation: string, version: number) {
    setLastResult(null);
    dryRun.mutate({ kind: "deployment", operation, sourceId: null, version });
  }

  function startOverrideSet(sourceId: string, version: number) {
    setLastResult(null);
    dryRun.mutate({
      kind: "override-set",
      operation: "chunk_summary",
      sourceId,
      version,
    });
  }

  function startOverrideClear(sourceId: string) {
    setLastResult(null);
    dryRun.mutate({
      kind: "override-clear",
      operation: "chunk_summary",
      sourceId,
      version: 0,
    });
  }

  function cancel() {
    setPending(null);
    dryRun.reset();
  }

  // Admin-gated endpoint: a 401/403 means "not authorized", not a real error.
  const forbidden =
    query.error instanceof ApiError &&
    (query.error.status === 401 || query.error.status === 403);

  const dryRunError =
    dryRun.error instanceof ApiError ? dryRun.error : null;
  const validVersions = dryRunError ? validVersionsFromError(dryRunError) : [];

  return (
    <section>
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Prompts</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Registered prompt versions and pins (admin only).
        </p>
      </div>

      {forbidden ? (
        <AdminRequired />
      ) : query.isLoading ? (
        <p className="mt-6 text-sm text-muted-foreground">
          Loading prompt versions…
        </p>
      ) : query.isError ? (
        <p
          role="alert"
          className="mt-6 rounded-md border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger"
        >
          {query.error instanceof ApiError
            ? query.error.message
            : "Failed to load prompt versions."}
        </p>
      ) : (
        <>
          <OperationsSection
            data={query.data}
            onSetDeploymentPin={startDeploymentPin}
          />

          {dryRunError && (
            <p
              role="alert"
              className="mt-4 rounded-md border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger"
            >
              {dryRunError.message}
              {validVersions.length > 0 && (
                <>
                  {" "}
                  Valid versions: {validVersions.join(", ")}.
                </>
              )}
            </p>
          )}

          {pending && (
            <ConfirmPanel
              pending={pending}
              onCancel={cancel}
              onConfirm={() => confirm.mutate()}
              confirming={confirm.isPending}
              confirmError={
                confirm.error instanceof ApiError ? confirm.error.message : null
              }
            />
          )}

          {lastResult && <LastResultSummary result={lastResult} />}

          <SourcesSection
            data={query.data}
            operations={query.data?.operations}
            onOverrideSet={startOverrideSet}
            onOverrideClear={startOverrideClear}
            onRefresh={(id) => {
              setRefreshResult(null);
              refresh.mutate(id);
            }}
            refreshingId={refresh.isPending ? (refresh.variables ?? null) : null}
            refreshError={
              refresh.error instanceof ApiError ? refresh.error.message : null
            }
            refreshResult={refreshResult}
          />
        </>
      )}
    </section>
  );
}

function AdminRequired() {
  return (
    <p
      role="alert"
      className="mt-6 rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning"
    >
      Admin access required to manage prompt versions.
    </p>
  );
}

function OperationsSection({
  data,
  onSetDeploymentPin,
}: {
  data: PromptVersionsResponse | undefined;
  onSetDeploymentPin: (operation: string, version: number) => void;
}) {
  const operations = data?.operations ?? {};
  const names = Object.keys(operations);

  return (
    <div className="mt-6 grid gap-4 sm:grid-cols-2">
      {names.map((name) => (
        <OperationCard
          key={name}
          name={name}
          info={operations[name]}
          onSetDeploymentPin={onSetDeploymentPin}
        />
      ))}
    </div>
  );
}

function OperationCard({
  name,
  info,
  onSetDeploymentPin,
}: {
  name: string;
  info: OperationVersions;
  onSetDeploymentPin: (operation: string, version: number) => void;
}) {
  const pinFieldId = `pin-${name}`;
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-center gap-2">
        <h2 className="font-medium">{name}</h2>
        <Badge variant="outline">latest v{info.latest}</Badge>
      </div>

      <ul className="mt-3 space-y-1 text-sm">
        {info.versions.map((v) => (
          <li key={v.version} className="flex gap-2">
            <span className="tabular-nums text-muted-foreground">
              v{v.version}
            </span>
            <span>{v.notes}</span>
          </li>
        ))}
      </ul>

      <div className="mt-3 space-y-1.5">
        <label htmlFor={pinFieldId} className="text-sm font-medium">
          {name} deployment pin
        </label>
        <Select
          id={pinFieldId}
          value={info.deployment_pin.version}
          onChange={(e) => onSetDeploymentPin(name, Number(e.target.value))}
          className="w-28"
        >
          {info.versions.map((v) => (
            <option key={v.version} value={v.version}>
              v{v.version}
            </option>
          ))}
        </Select>
      </div>
    </div>
  );
}

function ConfirmPanel({
  pending,
  onCancel,
  onConfirm,
  confirming,
  confirmError,
}: {
  pending: PendingAction;
  onCancel: () => void;
  onConfirm: () => void;
  confirming: boolean;
  confirmError: string | null;
}) {
  const { result } = pending;
  return (
    <div
      role="region"
      aria-label="Confirm prompt change"
      className="mt-4 rounded-lg border border-warning/40 bg-warning/10 p-4"
    >
      <p className="text-sm font-medium">
        {result.operation}: v{result.previous_version} → v{result.version}
        {pending.sourceId ? ` (source ${pending.sourceId})` : ""}
      </p>
      {result.effect && (
        <p className="mt-1 text-sm text-muted-foreground">{result.effect}</p>
      )}

      <ResultGroup title="Enqueued" items={result.enqueued} />
      <ResultGroup title="Deferred" items={result.deferred} showBlocking />
      <ResultGroup title="Not enqueued" items={result.not_enqueued} showReason />

      <p className="mt-2 text-sm tabular-nums">
        Total chunks: {formatChunks(result.total_chunks)}
      </p>

      {confirmError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {confirmError}
        </p>
      )}

      <div className="mt-3 flex gap-2">
        <Button variant="outline" size="sm" onClick={onCancel}>
          Cancel
        </Button>
        <Button size="sm" onClick={onConfirm} disabled={confirming}>
          {confirming ? "Applying…" : "Confirm"}
        </Button>
      </div>
    </div>
  );
}

function ResultGroup({
  title,
  items,
  showBlocking,
  showReason,
}: {
  title: string;
  items: PinResultItem[];
  showBlocking?: boolean;
  showReason?: boolean;
}) {
  if (items.length === 0) return null;
  return (
    <div className="mt-2 text-sm">
      <span className="font-medium">{title}</span>
      <ul className="mt-1 space-y-1">
        {items.map((item) => (
          <li key={item.source_id} className="text-muted-foreground">
            {item.source_id}: v{item.current_version} → v{item.target_version} (
            {formatChunks(item.chunk_count)} chunks)
            {item.job_id ? ` — job ${item.job_id}` : ""}
            {showBlocking && item.blocking_job_id
              ? ` — waiting on job ${item.blocking_job_id}`
              : ""}
            {showReason && item.reason ? ` — ${item.reason}` : ""}
          </li>
        ))}
      </ul>
    </div>
  );
}

function LastResultSummary({ result }: { result: PinResult }) {
  return (
    <div className="mt-4 rounded-lg border border-success/40 bg-success/10 p-4 text-sm">
      <p className="font-medium">
        Applied {result.operation}: v{result.previous_version} → v{result.version}
      </p>
      <ResultGroup title="Enqueued" items={result.enqueued} />
      <ResultGroup title="Deferred" items={result.deferred} showBlocking />
      <ResultGroup title="Not enqueued" items={result.not_enqueued} showReason />
      <p className="mt-2 tabular-nums">
        Total chunks: {formatChunks(result.total_chunks)}
      </p>
    </div>
  );
}

function SourcesSection({
  data,
  operations,
  onOverrideSet,
  onOverrideClear,
  onRefresh,
  refreshingId,
  refreshError,
  refreshResult,
}: {
  data: PromptVersionsResponse | undefined;
  operations: Record<string, OperationVersions> | undefined;
  onOverrideSet: (sourceId: string, version: number) => void;
  onOverrideClear: (sourceId: string) => void;
  onRefresh: (sourceId: string) => void;
  refreshingId: string | null;
  refreshError: string | null;
  refreshResult: { sourceId: string; jobId: string | null; reason?: string } | null;
}) {
  const sources = data?.sources ?? [];
  const chunkSummaryVersions = operations?.chunk_summary?.versions ?? [];

  return (
    <div className="mt-6">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="font-medium">Sources</h2>
        <span className="text-sm text-muted-foreground">
          {staleCount(sources)} stale
        </span>
      </div>

      {refreshError && (
        <p role="alert" className="mb-3 text-sm text-danger">
          {refreshError}
        </p>
      )}

      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-sm" aria-label="Sources">
          <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-4 py-2 font-medium">Source</th>
              <th className="px-4 py-2 font-medium">Chunks</th>
              <th className="px-4 py-2 font-medium">Built</th>
              <th className="px-4 py-2 font-medium">Target</th>
              <th className="px-4 py-2 font-medium">Status</th>
              <th className="px-4 py-2 font-medium">Override</th>
              <th className="px-4 py-2 font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {sources.map((s) => (
              <SourceRow
                key={s.source_id}
                source={s}
                versions={chunkSummaryVersions}
                onOverrideSet={onOverrideSet}
                onOverrideClear={onOverrideClear}
                onRefresh={onRefresh}
                refreshing={refreshingId === s.source_id}
                refreshResult={
                  refreshResult && refreshResult.sourceId === s.source_id
                    ? refreshResult
                    : null
                }
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SourceRow({
  source,
  versions,
  onOverrideSet,
  onOverrideClear,
  onRefresh,
  refreshing,
  refreshResult,
}: {
  source: PromptSourceRow;
  versions: { version: number; notes: string }[];
  onOverrideSet: (sourceId: string, version: number) => void;
  onOverrideClear: (sourceId: string) => void;
  onRefresh: (sourceId: string) => void;
  refreshing: boolean;
  refreshResult: { jobId: string | null; reason?: string } | null;
}) {
  const overrideFieldId = `override-${source.source_id}`;
  return (
    <tr className="border-t border-border">
      <td className="px-4 py-3">{source.label}</td>
      <td className="px-4 py-3 tabular-nums">
        {formatChunks(source.chunk_count)}
      </td>
      <td className="px-4 py-3 tabular-nums">
        {source.summary_prompt_version ?? "—"}
      </td>
      <td className="px-4 py-3 tabular-nums">{source.effective_version}</td>
      <td className="px-4 py-3">
        {source.stale ? (
          <Badge variant="warning">stale</Badge>
        ) : (
          <Badge variant="success">current</Badge>
        )}
      </td>
      <td className="px-4 py-3">
        <div className="flex items-center gap-2">
          <Select
            id={overrideFieldId}
            aria-label={`Override for ${source.label}`}
            value={source.override?.version ?? ""}
            onChange={(e) => {
              const v = e.target.value;
              if (v !== "") onOverrideSet(source.source_id, Number(v));
            }}
            className="w-28"
          >
            <option value="">Deployment pin</option>
            {versions.map((v) => (
              <option key={v.version} value={v.version}>
                v{v.version}
              </option>
            ))}
          </Select>
          {source.override && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => onOverrideClear(source.source_id)}
            >
              Clear
            </Button>
          )}
        </div>
      </td>
      <td className="px-4 py-3">
        <Button
          variant="outline"
          size="sm"
          disabled={refreshing}
          onClick={() => onRefresh(source.source_id)}
        >
          {refreshing ? "Refreshing…" : "Refresh"}
        </Button>
        {refreshResult && (
          <p className="mt-1 text-xs text-muted-foreground">
            {refreshResult.jobId
              ? `job ${refreshResult.jobId}`
              : (refreshResult.reason ?? "no-op")}
          </p>
        )}
      </td>
    </tr>
  );
}
