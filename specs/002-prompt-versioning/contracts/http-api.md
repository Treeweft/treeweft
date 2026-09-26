# HTTP API: prompt versions and pins

Every endpoint in this file:
- requires an admin through `authz._require_admin` (401 or 403 otherwise);
- returns 503 `{"detail": "prompt pins need Postgres (DATABASE_URL)"}` when Postgres is not
  configured.

Every mutation records `updated_by` from `_caller_id(request)`. All endpoints are additive to the
API contract (MINOR). The MCP tool surface is unchanged.

## GET /prompt-versions

200:

```json
{
  "operations": {
    "chunk_summary": {
      "versions": [{"version": 3, "notes": "Nonce-fenced one-sentence summary"}],
      "latest": 3,
      "deployment_pin": {"version": 3, "updated_at": "2026-09-25T12:00:00Z", "updated_by": null}
    },
    "hyde": {
      "versions": [{"version": 1, "notes": "Hypothetical snippet"}],
      "latest": 1,
      "deployment_pin": {"version": 1, "updated_at": "…", "updated_by": null}
    }
  },
  "sources": [
    {
      "source_id": "src_…",
      "label": "org/repo",
      "chunk_count": 18234,
      "summary_prompt_version": 3,
      "summary_refresh_target": null,
      "override": null,
      "effective_version": 3,
      "stale": false,
      "active_job_id": null
    }
  ]
}
```

- `override` is `{"version", "updated_at", "updated_by"}` or null.
- `label` is the source's URL or path, as `/sources` shows it.

## PUT /prompt-pins/{operation}?dry_run=false

This sets the deployment pin. The body is `{"version": N}`.

200 (real call):

```json
{
  "dry_run": false,
  "operation": "chunk_summary",
  "scope": "deployment",
  "previous_version": 3,
  "version": 4,
  "enqueued": [{"source_id": "…", "job_id": "…", "current_version": 3, "target_version": 4, "chunk_count": 18234}],
  "deferred": [{"source_id": "…", "blocking_job_id": "…", "current_version": 3, "target_version": 4, "chunk_count": 912}],
  "not_enqueued": [{"source_id": "…", "reason": "index is reindex_required: …", "current_version": 3, "target_version": 4, "chunk_count": 40}],
  "group_id": "…",
  "total_chunks": 19186,
  "effect": null
}
```

- `enqueued`: jobs created now.
- `deferred`: a source with an active job. Its refresh is enqueued when that job finishes
  (research R4).
- `not_enqueued`: the index is not writable (FR-017). The pin is still stored.
- `group_id`: set when two or more jobs are enqueued; otherwise null.
- `total_chunks`: the sum over all three lists.
- `effect`: for `hyde` it is `"takes effect on the next query"`, and all three lists are empty.
- Setting the pin to its current version is a no-op, and nothing is written.

200 (`dry_run=true`): the same shape, with `"dry_run": true`.
- `enqueued` lists the sources that **would** be enqueued, with no `job_id`.
- `deferred` and `not_enqueued` are computed the same way.
- `group_id` is null.
- No row is written, no `NOTIFY` is sent, and no job is created.

Errors:
- 400 `{"detail": "unknown chunk_summary version 7", "valid_versions": [3, 4]}`;
- 404 for an unknown operation.

## PUT /prompt-pins/chunk_summary/sources/{source_id}?dry_run=false

This sets a per-source override. The body is `{"version": N}`. It returns 200 in the same shape
as above, with `"scope": "<source_id>"` and at most one source across the three lists.

Errors:
- 400 for an unknown version (with `valid_versions`);
- 404 for an unknown source.

## PUT /prompt-pins/hyde/sources/{source_id}

It returns 400 `{"detail": "hyde pins are deployment-wide; per-source overrides are not supported"}`,
as does a request with `?dry_run=true`. The table's `CHECK` enforces the same rule.

## DELETE /prompt-pins/chunk_summary/sources/{source_id}?dry_run=false

This clears an override; the source then follows the deployment pin. It returns 200 in the same
shape, with `"version"` set to the deployment pin the source now follows.

Errors: 404 when the source is unknown or has no override.

## POST /sources/{source_id}/resummarize

A manual refresh, with no body.

- 202 `{"job_id": "…", "target_version": 4}`: enqueued.
- 200 `{"job_id": null, "reason": "already current at chunk_summary v4"}`: nothing to do.
- 200 `{"job_id": "<active>", "deferred": true}`: another job is active. The refresh is enqueued
  when that job finishes.
- 409: the index guard's body when the index is not writable (ADR-004 §3).
- 404: unknown source.

A source whose `summary_refresh_target` is set counts as stale (FR-015), so a manual refresh
always runs for it.

## Changes to existing responses (additive)

- `GET /sources`: each source gains `summary_prompt_version`, `summary_refresh_target` and
  `summary_stale`.
- `GET /jobs` and `GET /jobs/{id}`: a new `kind` value, `resummarize`. The job-group kind
  `prompt-refresh` is new too. Both kind fields are free text, so this is not a contract change.
