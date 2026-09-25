# Contract: Indexer HTTP API changes

Additive to the 1.0.0 surface: new optional response fields and one new endpoint, a SemVer MINOR
change (research R10).

## `GET /health` (public, no auth). New fields

```json
{"status": "ok", "database": "connected", "auth_enabled": true,
 "version": "1.1.0", "release": null,
 "index_schema": 1,
 "index_status": "reindex_required",
 "reindex_reason": "vector store (milvus): embedding_model is BAAI/bge-m3, configured Qwen/Qwen3-Embedding-0.6B"}
```

| Field | Type | When |
|---|---|---|
| `index_schema` | int | always: the running `INDEX_SCHEMA_VERSION` |
| `index_status` | `"ok"`, `"unverified"`, `"reindex_required"` or `"rebuilding"` | always |
| `reindex_reason` | string | `index_status` is `reindex_required` or `unverified` (for `unverified` it says what could not be verified) |
| `rebuild_progress` | `{"done": int, "total": int}` | `index_status` is `rebuilding` |

- `status` stays `"ok"` in every index state.
- The response is served from in-memory state and never blocks on a store or on the embedding
  service.

## Refusals while the index is not usable

These routes return **409**, with the body in [errors.md](errors.md):

| State | Refused routes |
|---|---|
| `reindex_required` | `POST /search`, `POST /hydrate-chunks`, `GET /find-definition`, `GET /find-callers`, `GET /find-references`, `POST /graph-explore` |
| `reindex_required`, and `unverified` after one inline re-check | `POST /index-file`, `POST /index-directory`, `POST /index-repo`, `POST /index-graph`, `POST /jobs/{id}/retry`, `POST /build-community`, and the webhook routes that enqueue jobs |

- Unchanged in every state: `/health`, auth routes, `/sources`, `/fleet`, `/jobs`, `/job-groups`,
  admin routes, the UI.
- Authorization runs first. An unauthorized caller gets 401 or 403, not 409, so the index state is
  not disclosed beyond what the public `/health` already shows.

## `POST /index/rebuild` (admin only)

Query: `dry_run` (bool, default `false`).

**Dry run → 200**

```json
{"dry_run": true,
 "sources": [{"id": "src-1", "label": "https://git.example/app.git", "kind": "repo", "chunk_count": 18234}],
 "total_sources": 1, "total_chunks": 18234,
 "index_status": "reindex_required",
 "blockers": []}
```

`blockers` lists what would make the real call fail, such as `"2 index jobs queued or running"`,
`"community build running"` or `"rebuild group <id> in progress"`. Nothing is written.

**Real call → 202**

```json
{"dry_run": false, "group_id": "grp-…", "total_sources": 1, "total_chunks_before": 18234,
 "index_status": "rebuilding"}
```

**Errors**

| Code | When |
|---|---|
| 401 / 403 | not authenticated / not admin (`authz._require_admin`) |
| 409 | any blocker present. Body: `{"detail": "Rebuild refused: <blockers>", "blockers": [...]}`. Nothing dropped. |
| 503 | a store could not be dropped or recreated. Body names the store and step, and `index_status` becomes `reindex_required` with reason "rebuild failed at <step>". |

Progress is visible in `GET /health` (`rebuild_progress`) and `GET /job-groups/{group_id}`.

## Job failure at dispatch

A job that reaches a worker while writes are refused ends with status `failed` and
`error = "<detail string from errors.md>"`. It is not retried.
