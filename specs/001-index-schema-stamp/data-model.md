# Data Model: Index Schema Stamp

Phase 1 of [plan.md](plan.md). The pure types live in `src/treeweft/domain/index_stamp.py`. The
storage of each stamp is described in [research.md](research.md) R1.

## IndexStamp

What built a store's data.

| Field | Type | Rules |
|---|---|---|
| `schema` | int | ≥ 1. Compared with `INDEX_SCHEMA_VERSION` (`versions.py`, value 1). |
| `embedding_model` | str | Non-empty. Compared by exact string equality with `EMBEDDING_MODEL`. |
| `vector_dim` | int | ≥ 1. Compared with `VECTOR_DIM`. For Milvus and LanceDB it is read from the store's own schema, not from a stored key. |

Round-trip: every store serialises it to strings (Milvus properties, LanceDB field metadata,
Chroma metadata) or to native columns and properties (SQLite, Neo4j). A stamp that cannot be
parsed, such as a non-integer schema or a missing key, is a **mismatch** named "unreadable stamp".
It is never treated as "no stamp", because that would re-trigger adoption over corrupt data.

## ConfiguredIndex

What this process expects: `INDEX_SCHEMA_VERSION`, `EMBEDDING_MODEL`, `VECTOR_DIM`. It is built
once at startup from configuration.

## StoreObservation

What a store reports to the check. The adapters produce it; it holds no decisions.

| Field | Type | Meaning |
|---|---|---|
| `store` | `"vector"` or `"graph"` | Role |
| `backend` | str | `milvus`, `lancedb`, `chromadb`, `neo4j` or `sqlite`. Used in reasons. |
| `exists` | bool | Collection or table present (graph: always true after `ensure_schema`) |
| `has_data` | bool | At least one chunk (vector) or entity (graph) |
| `stamp` | IndexStamp or None | As read |
| `schema_dim` | int or None | Dimension from the store's schema, where it has one |
| `unreachable` | str or None | Error text if the store could not be read |

## StoreCheck (decision per store)

`decide_store(observation, configured, verification) -> StoreCheck`. `verification` is the legacy
result (R3): `passed`, `failed(check_name, detail)`, `unavailable(detail)` or `not_run`.

| Observation | Outcome | Action |
|---|---|---|
| `unreachable` set | `unverified` | none; retry later |
| collection absent (vector) | `ok` | none (`init_collection` stamps on create) |
| no data, no stamp | `ok` | write the stamp |
| stamp present, all fields equal | `ok` | none |
| stamp present, any field differs (including stored schema > current) | `reindex_required` | none; reason per differing field |
| stamp unreadable | `reindex_required` | none |
| data, no stamp, verification `passed` | `ok` | write the stamp (adopt as schema 1) |
| data, no stamp, verification `failed` (including "no verifiable chunks": none of up to 200 inspected rows is under 50,000 characters) | `reindex_required` | none; reason names the check |
| data, no stamp, verification `unavailable` | `unverified` | none |
| data, no stamp, verification `not_run` | `unverified` | none (never adopt without verifying) |
| graph: data, no stamp, vector store has no data | `reindex_required` | none; "graph cannot be verified without vector data" |

Reason format for one mismatch (FR-005):
`"<role> store (<backend>): <field> is <stored>, configured <configured>"`. For example:
`vector store (milvus): embedding_model is BAAI/bge-m3, configured Qwen/Qwen3-Embedding-0.6B`.
Several reasons are joined with `"; "`.

## RebuildState (shared, derived from Postgres)

Derived by `refresh()` in every process from the latest `index-rebuild` job group, its jobs and
the maintenance-lock probe (research R7, R13).

| Value | Condition | Reported as |
|---|---|---|
| `none` | no `index-rebuild` group ever | nothing |
| `preparing` | the maintenance lock is held **exclusively** | `index_status: rebuilding`, `rebuild_progress {done: 0, total: task_count}` (0 if no group yet) |
| `rebuilding` | the lock is not held exclusively, and some of the latest group's jobs are `queued`/`running` | `rebuilding` + progress |
| `interrupted` | the lock is not held exclusively, the latest group has fewer jobs than `task_count`, and none is active | `reindex_required`, reason "a rebuild was interrupted; run it again" |
| `complete` | all `task_count` jobs are terminal (`done`, `failed`, `dead_letter`), or `task_count == 0` | nothing (the stamp check decides) |

## IndexStatus (per-process cached view)

`aggregate(checks, rebuild_state) -> IndexStatus`, published by `refresh()` and read by
`/health` and the route gates.

| Field | Type | Present when |
|---|---|---|
| `state` | `ok`, `unverified`, `reindex_required` or `rebuilding` | always |
| `preparing` | bool (internal, not exposed) | `rebuild_state == preparing` |
| `reason` | str | `reindex_required` or `unverified` |
| `rebuild_progress` | `{"done": int, "total": int}` | `rebuilding` |
| `refreshed_at` | datetime | always (logged, not exposed) |

Precedence:

1. `preparing` (reported as `rebuilding`) comes first: the stores are being recreated, so no
   stamp check is meaningful.
2. Then `interrupted`, reported as `reindex_required`.
3. Then the store checks, worst first: `reindex_required` > `unverified`.
4. Then `rebuilding`.
5. Then `ok`.

Staleness: a process's view lags the shared truth by at most `INDEX_STATUS_REFRESH_SECONDS`
(default 5). The authoritative `dispatch_allowed()` never uses the cache for lock and group state.

### State transitions (reported `index_status`)

```text
                startup check
  (none) ──────────────────────────► ok | unverified | reindex_required | rebuilding (another process is preparing)
  unverified ──retry passes────────► ok
  unverified ──retry fails a check─► reindex_required
  reindex_required | ok | unverified ──POST /index/rebuild (any process)──► rebuilding (preparing, then jobs running)
  rebuilding ──group complete──────► ok            (failed sources listed in the group)
  rebuilding ──holder died before all jobs were enqueued──► reindex_required ("rebuild interrupted")
  reindex_required ──another process rebuilt; next refresh re-observes stamps──► rebuilding / ok
  any ──restart with changed config─► (startup check decides)
```

Allowed operations per state:

| State | Search/read endpoints | Index jobs and community build | Rebuild |
|---|---|---|---|
| ok | yes | yes | yes (if no blockers) |
| unverified | yes | no: inline re-check first, then 409 if still unverified | yes |
| reindex_required | 409 | 409 / job fails at dispatch | yes |
| rebuilding (preparing) | 409 ("stores are being recreated; retry shortly") | 409 / job fails at dispatch, except the rebuild group's own jobs | 409 (one already running) |
| rebuilding (jobs running) | yes (partial results) | yes | 409 (one already running) |

## Rebuild job group

This reuses `job_groups` (migration 020). No new table.

| Field | Value |
|---|---|
| `kind` | `"index-rebuild"` |
| `label` | `"index rebuild"` |
| `task_count` | number of sources at request time (preset, not incremented) |
| `created_by` | admin user id |

Its jobs are ordinary index jobs with `group_id` set, built the same way as
`_enqueue_source_reindex`. `done` counts jobs in `done`, `failed` or `dead_letter`. If the
blocker re-check (research R7 step 3) aborts the rebuild, the group is deleted
(`JobGroupStore.delete`) before the lock is released, so an aborted rebuild never reads as
`interrupted`.

## Maintenance lock (shared)

A Postgres session-level advisory lock with the two-integer key `(hashtext('treeweft'),
hashtext('index-maintenance'))`, held on a dedicated connection (research R13).

| Holder | Mode |
|---|---|
| Rebuild (steps 1–6) | exclusive |
| Community build (whole run) | shared |
| Stamp write by `run_check()` | shared (for the write only) |

## Graph meta structures (new)

- **Neo4j**: `(:TreeweftMeta {id: "index", index_schema, embedding_model, vector_dim, stamped_at})`.
  A uniqueness constraint `treeweft_meta_id_unique` is added to `_SCHEMA_STATEMENTS`.
- **SQLite**: `CREATE TABLE IF NOT EXISTS treeweft_meta (id TEXT PRIMARY KEY CHECK (id = 'index'),
  index_schema INTEGER NOT NULL, embedding_model TEXT NOT NULL, vector_dim INTEGER NOT NULL,
  stamped_at INTEGER NOT NULL)`, added to `_SCHEMA_STATEMENTS`.

Both are additive and created idempotently. They invalidate no existing data, so
`INDEX_SCHEMA_VERSION` stays 1 (research R9).
