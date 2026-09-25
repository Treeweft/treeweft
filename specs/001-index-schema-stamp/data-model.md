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
| data, no stamp, verification `failed` | `reindex_required` | none; reason names the check |
| data, no stamp, verification `unavailable` | `unverified` | none |
| data, no stamp, verification `not_run` | `unverified` | none (never adopt without verifying) |
| graph: data, no stamp, vector store has no data | `reindex_required` | none; "graph cannot be verified without vector data" |

Reason format for one mismatch (FR-005):
`"<role> store (<backend>): <field> is <stored>, configured <configured>"`. For example:
`vector store (milvus): embedding_model is BAAI/bge-m3, configured Qwen/Qwen3-Embedding-0.6B`.
Several reasons are joined with `"; "`.

## IndexStatus (process-wide)

`aggregate(checks, rebuild) -> IndexStatus`.

| Field | Type | Present when |
|---|---|---|
| `state` | `ok`, `unverified`, `reindex_required` or `rebuilding` | always |
| `reason` | str | `reindex_required` or `unverified` |
| `rebuild_progress` | `{"done": int, "total": int}` | `rebuilding` |
| `checked_at` | datetime | always (logged, not exposed) |

Precedence: `reindex_required` > `unverified` > `rebuilding` > `ok`. An interrupted rebuild (R7)
forces `reindex_required`.

### State transitions

```text
                startup check
  (none) ──────────────────────────► ok | unverified | reindex_required
  unverified ──retry passes────────► ok
  unverified ──retry fails a check─► reindex_required
  reindex_required ──POST /index/rebuild──► rebuilding
  ok ──POST /index/rebuild─────────► rebuilding
  rebuilding ──group complete──────► ok            (failed sources listed in the group)
  rebuilding ──restart, group incomplete, jobs missing──► reindex_required ("rebuild interrupted")
  any ──restart with changed config─► (startup check decides)
```

Allowed operations per state:

| State | Search/read endpoints | Index jobs and community build | Rebuild |
|---|---|---|---|
| ok | yes | yes | yes (if no active jobs) |
| unverified | yes | no: inline re-check first, then 409 if still unverified | yes |
| reindex_required | 409 | 409 / job fails at dispatch | yes |
| rebuilding | yes (partial results) | yes | 409 (one already running) |

## Rebuild job group

This reuses `job_groups` (migration 020). No new table.

| Field | Value |
|---|---|
| `kind` | `"index-rebuild"` |
| `label` | `"index rebuild"` |
| `task_count` | number of sources at request time (preset, not incremented) |
| `created_by` | admin user id |

Its jobs are ordinary index jobs with `group_id` set, built the same way as
`_enqueue_source_reindex`. `done` counts jobs in `done`, `failed` or `dead_letter`.

## Graph meta structures (new)

- **Neo4j**: `(:TreeweftMeta {id: "index", index_schema, embedding_model, vector_dim, stamped_at})`.
  A uniqueness constraint `treeweft_meta_id_unique` is added to `_SCHEMA_STATEMENTS`.
- **SQLite**: `CREATE TABLE IF NOT EXISTS treeweft_meta (id TEXT PRIMARY KEY CHECK (id = 'index'),
  index_schema INTEGER NOT NULL, embedding_model TEXT NOT NULL, vector_dim INTEGER NOT NULL,
  stamped_at INTEGER NOT NULL)`, added to `_SCHEMA_STATEMENTS`.

Both are additive and created idempotently. They invalidate no existing data, so
`INDEX_SCHEMA_VERSION` stays 1 (research R9).
