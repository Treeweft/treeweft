# Research: Index Schema Stamp, Reindex-Required Mode and Rebuild

Phase 0 of [plan.md](plan.md). Each item records a decision, why it was made, and what else was
considered. File references are to the tree at `02786ba`. Pymilvus server behaviour marked
*unverified* could not be checked live because the local Milvus was down during research; the
Milvus integration test (T-level detail in `tasks.md`) is the gate for it.

## R1. Where the stamp lives in each store

**Decision**

| Store | Stamp location | Dimension read from |
|---|---|---|
| Milvus | Collection properties `treeweft.index_schema`, `treeweft.embedding_model` (`alter_collection_properties`; `create_collection(properties=…)` on create) | The `vector` field's `params.dim` in `describe_collection()` |
| LanceDB | Field metadata on the `vector` field (`tbl.replace_field_metadata("vector", {...})`), keys `treeweft.index_schema`, `treeweft.embedding_model` | `tbl.schema.field("vector").type.list_size` |
| ChromaDB | Collection metadata keys `treeweft.index_schema`, `treeweft.embedding_model`, `treeweft.vector_dim` (merged with the existing metadata; never drops `hnsw:*` keys) | The stamp (Chroma has no declared dimension; a stored embedding's length is the legacy check) |
| Neo4j | One `(:TreeweftMeta {id: "index"})` node with `index_schema`, `embedding_model`, `vector_dim` | The stamp |
| SQLite graph | One-row `treeweft_meta` table (`id TEXT PRIMARY KEY CHECK (id = 'index')`, `index_schema INTEGER`, `embedding_model TEXT`, `vector_dim INTEGER`) | The stamp |

**Rationale**

- Milvus: pymilvus 3.0.0 is installed (uv.lock). `alter_collection_properties`,
  `describe_collection()["properties"]` (str→str), and `create_collection(properties=)` all exist
  (verified by introspection). Property values come back as strings, so `index_schema` is parsed
  with `int()`. That Milvus 2.5.4 accepts arbitrary `treeweft.*` keys is *unverified*; the
  integration test covers it.
- LanceDB: ADR-004 §3 says "table schema metadata (feasibility to be confirmed during planning)".
  It is only partly feasible. lancedb 0.33.0 accepts schema-level metadata at `create_table`, but a
  `LanceTable` has no call that changes it later, so a table created before 1.0.0 could never be
  adopted. `replace_field_metadata` works after creation and survives adds, deletes, FTS index
  creation, `optimize()` and reopening (verified empirically). One mechanism for new and adopted
  tables beats two. Field metadata is still table schema metadata, so this satisfies ADR-004's
  wording and needs no ADR amendment. `replace_field_metadata` replaces rather than merges, so the
  writer reads the existing field metadata first and merges into it.
- ChromaDB is a third selectable vector store (`VECTOR_STORE=chromadb`, `retriever.py:26`) that
  ADR-004's table does not list. Spec FR-002 covers "every supported vector store", so it gets a
  stamp too. Collection metadata is its natural place. chromadb 1.5.9 is installed; whether
  `collection.modify(metadata=…)` merges or replaces, and whether it rejects changes that touch
  `hnsw:*`, gets pinned down by a unit test on an embedded temp-dir client. That is allowed:
  constitution II permits in-process stores on a temp file.
- Graph stores: as ADR-004 specifies. Neo4j's `clear_all()` (`MATCH (n) DETACH DELETE n`) would
  delete the meta node, and so would the rebuild's delete step. Both exclude `:TreeweftMeta` (R7).
  SQLite's `clear_all()` lists tables explicitly and never touches `treeweft_meta`.

**Alternatives considered**

- A LanceDB sidecar JSON file next to the table. Rejected: it can drift from the table when one
  is copied without the other, which is exactly the failure stamping the store itself prevents.
- Recording the stamp in Postgres. Rejected by ADR-004.

## R2. When the check runs, and what "fresh" means for lazily created stores

**Decision**

- The vector collection is created lazily (`init_collection()` runs inside the job runners, never
  at startup; `indexer_runners.py:612, 980, 1055, 1520`), so the startup check never creates it:
  - **Collection absent** counts as fresh. The store reports `ok` with nothing to stamp yet.
    `init_collection()` writes the stamp when it creates the collection (Milvus
    `create_collection(properties=…)`, LanceDB `create_table` then field metadata, Chroma
    `get_or_create_collection(metadata=…)`).
  - **Collection present, zero rows, no stamp** counts as fresh too. The check writes the stamp.
- The graph schema is ensured at startup (`lifecycle.py:413-419`), so a graph store with no data
  and no stamp is stamped at startup.
- The check runs in `lifecycle.startup()` after `graph_store.ensure_schema()` and after the
  embedding proxy is created, which legacy verification needs (`lifecycle.py:425-439`). It runs
  **before** `JobStore().init()` and the queue workers start (`:450-457`), and before recovered
  jobs are re-enqueued (`:460-512`). No job can write before the status is known.
- The check's own I/O is bounded: store reads use the adapters' normal timeouts, and legacy
  re-embedding is capped by `INDEX_VERIFY_TIMEOUT_SECONDS` (default 15). If the check cannot
  complete because a store or the embedding service is unreachable, the status is `unverified`
  with a reason naming what could not be reached. The FR-011 retry loop then covers it, and
  startup is never blocked or failed by an unreachable backing service. That matches today's
  behaviour: the indexer starts when Milvus is down.

**Rationale**: Putting the stamp in `init_collection` covers every creation path (every runner,
the rebuild) in one place. Running the check before workers start closes the window in which a
recovered job could write into an unchecked index.

**Alternatives considered**: Creating the collection eagerly at startup. Rejected: it makes
startup depend on Milvus, and simple-mode startup is guarded to stay adapter-free (constitution
IV).

## R3. Legacy verification (data but no stamp)

**Decision**

1. **Dimension**:
   - Milvus: the collection schema's `vector` dim.
   - LanceDB: `list_size`.
   - Chroma: the length of a stored embedding.

   Each must equal `VECTOR_DIM`.
2. **Model**: sample up to 3 stored rows whose `chunk_text` is shorter than 50 000 characters.
   Longer rows are stored truncated (`vector_store.py:204-223`), so their text no longer matches
   what was embedded. Re-embed the texts with `embedder.embed()`, **not** `embed_query()`, which
   adds `EMBEDDING_QUERY_PREFIX`. Compare each result with the row's stored `vector` by cosine
   similarity. Every sample must reach ≥ 0.99. `summary_vector` is never compared: it holds
   summary embeddings.
   - Milvus: `query(filter="", output_fields=["chunk_text","vector"], limit=20)` and pick 3.
   - LanceDB: `search().limit(20).select([...])`.
   - Chroma: `get(limit=20, include=["documents","embeddings"])`.
3. **Graph store**: an unstamped graph store with data is adopted only when the vector store's
   verification passed in the same check. If the vector store holds no data, the graph cannot be
   verified, so the result is `reindex_required` (spec edge case).
4. Every result, pass or fail, is logged with the per-sample cosine values.

**Rationale**:

- This is ADR-004's rule, sharpened by two facts found in the code: the truncation mismatch and
  the query prefix. Either one would make a correct index fail verification.
- Chunk vectors embed the raw chunk text with no contextual header (`indexer_runners.py:584-593`),
  so re-embedding reproduces them.
- The TEI proxy normalises vectors (`normalize: True`); cosine is scale-invariant regardless.

**Known limit** (documented in `docs/upgrading.md`): the TEI path does not send `EMBEDDING_MODEL`
to the server (`embedding_proxy.py:84` uses it only for a tokenizer). So a stamped index cannot
detect TEI being switched to a different model under the same configured name. The stamp compares
configuration, as ADR-004 specifies; legacy verification is the only place the served model is
tested.

**Alternatives considered**: Re-verifying the served model on every startup. Rejected: it adds
embedding load and a startup dependency for a case ADR-004 does not cover. A candidate follow-up
issue.

## R4. Where the index status lives; the retry loop

**Decision**

- **Pure logic in `src/treeweft/domain/index_stamp.py`** (no adapter imports):
  - `IndexStamp`, `ConfiguredIndex`, `StoreCheck`, `IndexStatus`
  - `decide_store(...)`: the ADR-004 §3 decision table per store
  - `aggregate(...)`: worst-of across stores, with `reindex_required` > `unverified` >
    `rebuilding` > `ok`
- **Runtime state and orchestration in `src/treeweft/application/index_guard.py`**:
  - the current `IndexStatus` held in a module global, the same style as `indexer_state.py`
  - `run_check()`
  - the verification retry loop
  - `require_searchable()` / `require_writable()`
  - rebuild status derivation
- **Retry loop**: an asyncio task started in `lifecycle.startup()` and cancelled in `shutdown()`,
  like the existing background loops (`lifecycle.py:516-545`). It runs `run_check()` every
  `INDEX_VERIFY_INTERVAL_SECONDS` (default 60) while the status is `unverified`, and is idle
  otherwise.
  - `require_writable()` also runs `run_check()` inline, once, before refusing a job while
    `unverified`.
  - An `asyncio.Lock` serialises checks.

**Rationale**:

- Keeping the decision table in `domain/` makes it unit-testable with plain values, which is
  constitution IV layering.
- Module-level state matches how the indexer already holds its runtime state. `wiring.py` is dead
  code (it imports classes that no longer exist) and is not a model to follow.

**Alternatives considered**: Storing the status in Postgres. Rejected: it has to reflect the stores
as seen by this process. Simple mode may run without Postgres.

## R5. How endpoints and jobs are gated

**Decision**: two layers.

1. **HTTP layer (the 409 the caller sees).**
   - `index_guard.require_searchable()` is called at the top of `/search`, `/hydrate-chunks`,
     `/find-definition`, `/find-callers`, `/find-references` and `/graph-explore`.
   - `index_guard.require_writable()` is called at the top of `/index-file`, `/index-directory`,
     `/index-repo`, `/index-graph`, `/jobs/{id}/retry`, `POST /build-community` (an in-process
     graph write, not a queued job) and the webhook routes that enqueue jobs
     (`routes_webhook.py:263, 316, 339`).
   - Both raise the 409 described in `contracts/errors.md`.
   - No route in the indexer uses `Depends` today, and `_authorize_scope` cannot be the gate: it
     returns early when auth is off and also guards write routes. Explicit calls follow the
     existing style and are enforced by a route-table test (next point).
2. **Execution choke point (defence in depth).** The queue worker
   (`adapters/queue/postgres_queue.py` `_run_one`) checks `index_guard.writes_allowed()` just
   before `dispatch_job`. A job reaching it while writes are refused is marked `failed` with the
   reason, the same path an undispatchable job takes today (`:132-140`). Attempts are not
   incremented, so the job is not retried. This covers every path the HTTP
   layer cannot see: fleet auto-refresh (`_enqueue_source_reindex`), restart recovery, and jobs
   queued before a status change.
3. **A route-table test.** It asserts that every route in the read set calls
   `require_searchable` and every route in the write set calls `require_writable`, by inspecting
   route endpoint source. Any new route under `/index-*`, `/find-*`, `/search*` or `/graph-*`
   must appear in one of the sets or in an explicit exempt list. This keeps the gate from
   silently missing a future endpoint (constitution V).

**Rebuild jobs are allowed**: `writes_allowed()` is true while `index_status` is `ok` or
`rebuilding`. It is false while `reindex_required` or `unverified`. The rebuild's own jobs only
start after the stores are recreated and stamped, when the status is already `rebuilding`.

**Alternatives considered**: One middleware that matches paths. Rejected: path matching is
implicit and would also have to understand the webhook router. Explicit calls plus a test are
clearer.

## R6. The 409 body

**Decision**:

```json
{"detail": "Index requires rebuild: <reason>. Run POST /index/rebuild?dry_run=true, then POST /index/rebuild (docs/upgrading.md).",
 "reason": "<reason>", "index_status": "reindex_required", "rebuild": "/index/rebuild"}
```

- The `unverified` variant (index jobs only) says the index is unverified and why.
- `detail` stays a string. The MCP error mapping reads only `detail` and truncates it to 300
  characters (`mcp_compat.py:95-106`). So the actionable text leads, and the reason is capped
  so the pointer survives truncation.

**Rationale**: Agents only ever see `detail`, so the pointer must live there. Structured fields
serve the UI and scripts.

## R7. Rebuild orchestration

**Decision**: `POST /index/rebuild` (admin only, `authz._require_admin`), `?dry_run=true`.

- **Dry run** returns each source from `source_records` (`id`, `label`, `kind`, `chunk_count`)
  plus totals, and the blockers that would make the real call fail. It writes nothing.
- **Preconditions for the real call**, all checked before anything is dropped. If any fails, the
  call returns 409 listing the blockers:
  - no job `queued` or `running` (`JobStore.list_by_status`);
  - no community backfill running (`_state._community_build_state`);
  - no rebuild group still incomplete.
- **Order**, chosen so a crash at any step fails loud rather than silently leaving an empty `ok`
  index:
  1. Create a job group with `kind="index-rebuild"` and `task_count` preset to the number of
     sources (not incremented per attach).
  2. Drop the vector collection, run `init_collection()` (which stamps it, R2), clear the graph
     index data, then stamp the graph (Neo4j: batched
     `MATCH (n) WHERE NOT n:TreeweftMeta CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF
     10000 ROWS` in an auto-commit session; SQLite: `clear_all()`, which already spares
     `treeweft_meta`). Then call `retrieval.invalidate_graph_caches()`.
  3. Set the status to `rebuilding`.
  4. For each source, build the job exactly as `_enqueue_source_reindex` does (same kind
     resolution), set `group_id`, persist it and enqueue it.
- **Graph clear scope**: the whole graph except the meta node, including `Source` nodes. The job
  runners recreate them by `MERGE`. The fleet list reads `source_records` first; the graph
  `Source` fallback is only used when Postgres is down.
- **Status derivation**, used at startup and on every `/health` read (a cached
  `summarize_group`, refreshed at most every 5 s):
  - Take the latest `index-rebuild` group, if any.
    - If it has fewer jobs than its preset `task_count` and none of them is active, the rebuild
      was interrupted: status `reindex_required`, reason "a rebuild was interrupted; run it
      again".
    - If any of its jobs is queued or running: `rebuilding`, with
      `rebuild_progress = {"done": done+failed+dead_letter, "total": task_count}`.
    - Otherwise the rebuild is complete, and the stamp check alone decides.
  - A group with `task_count == 0` (no sources) is complete immediately.
- **No `force` flag needed**: the "already indexed, skipping" check exists only in the HTTP
  routes (`indexer_service.py:796, 831, 888`). The runners' resume list comes from the vector
  store (`list_indexed_paths`, `indexer_runners.py:646-654`), which is empty after the drop.
- **Kept**: the summary cache (Postgres `summary_cache`, keyed `(sha1, model, prompt_version)`,
  migration 005), `source_records`, users, groups, keys and job history.

**Alternatives considered**:

- Enqueueing the jobs before dropping the stores. Rejected: a worker could write into the old
  collection before the drop.
- A new Postgres table for rebuild state. Rejected: `job_groups.kind` plus a preset `task_count`
  carry everything, with no migration.

## R8. Logging the rebuild (spec FR-017)

**Decision**: The indexer has no audit trail for admin operations. `infrastructure/audit.py`
covers LLM operations and `_audit_search` covers searches. So the rebuild emits one structured
`logger.warning` per call with `event=index_rebuild`, `user`, `dry_run`, `sources`,
`total_chunks` and `group_id`. The real call also logs each stage (group created, stores dropped,
stamped, N jobs enqueued). FR-017 in the spec is reworded to match: "logged with the caller"
rather than "like other admin operations".

**Alternatives considered**: Building an admin audit trail. Rejected: out of scope, and no other
admin operation has one. A candidate follow-up issue.

## R9. The index-schema contract snapshot

**Decision**

- **`contracts/index_schema.json`**: `{"source_version", "index_schema", "surface"}`. The surface
  has these sections:
  - `milvus`: fields, indexes, functions, and the stamp property keys;
  - `lancedb`: arrow schema and FTS column;
  - `chromadb`: stamp keys;
  - `neo4j`: `_SCHEMA_STATEMENTS` plus the meta-node shape;
  - `sqlite`: `_SCHEMA_STATEMENTS`, including `treeweft_meta`.
- **Dimension placeholder**: dimensions are built with a fixed probe value and replaced with the
  placeholder `"<VECTOR_DIM>"`, so the `VECTOR_DIM` from `.env` never leaks into the snapshot.
- **Refactors so the surface builds with no services**:
  - Milvus: extract `build_collection_schema(dim)` and `INDEX_PARAMS` to module level in
    `adapters/milvus/vector_store.py`; `init_collection` uses them.
  - LanceDB: `_schema(dim)` takes the dimension; add a module constant `FTS_COLUMN`.
  - Neo4j and SQLite: `_SCHEMA_STATEMENTS` are already importable.
- **`infrastructure/contracts.py`** gains:
  - `index_schema_surface()`, which imports the adapters **lazily inside the function**, so the
    module stays off the startup path (the CLAUDE.md invariant);
  - `diff_index(old, new)`: any difference is `Change(kind="index-breaking", ...)`;
  - `index_version_error(recorded_schema, current_schema, released, current)`.
- **`write_snapshot`** gains an optional `extra` dict, so the file can record `index_schema`.
- **`scripts/update_contracts.py`** writes the third snapshot.
- **`tests/unit/test_contracts.py`** gains the index case: a synthetic-change table, and the real
  snapshot check. Unit tests may import the adapters because CI copies `.env.example` to `.env`
  (`ci.yml:104-110`) and nothing connects at import.
- **This PR creates `contracts/index_schema.json`** as the baseline at schema 1, recording
  `source_version` `"1.0.0"`, the last release. ADR-004 says snapshots are regenerated only in
  release PRs; this one did not exist, and its baseline has to exist before the test can compare
  against it.
  - The only schema difference from 1.0.0 is the new meta structures (the `treeweft_meta` table
    and the `TreeweftMeta` node). Those are additive and invalidate no data, so
    `INDEX_SCHEMA_VERSION` stays 1.

## R10. Version bump in this PR

**Decision**:

- `POST /index/rebuild` is a new endpoint, so the existing API contract test classifies the PR as
  additive and requires `pyproject.toml` > 1.0.0 at minor level (`test_contracts.py:139-154`).
- This PR therefore sets `pyproject.toml` to **1.1.0** (and `uv.lock` follows) and adds a
  `CHANGELOG.md` "Unreleased" entry under "Added".
- It does **not** regenerate `api.json` or `mcp_tools.json`; that is the release PR's job, per
  ADR-004 §4.
- The spec's assumption ("releasing is a separate PR") still holds for tagging and snapshots. The
  assumption's wording is corrected: the version bump is forced into this PR.

## R11. UI health indicator

**Decision**:

- `ui/src/components/layout/HealthIndicator.tsx` extends `HealthResponse` with `index_status`,
  `reindex_reason` and `rebuild_progress`.
- `healthStatus()` maps:
  - `reindex_required` → error state, label "Re-index required", tooltip the reason;
  - `unverified` → warning, "Index unverified";
  - `rebuilding` → info, "Rebuilding n/m".
- `status` keeps its current meaning.
- Verification: the UI has no test for this component today. Vitest is configured (`npm test`
  runs `vitest run`), so this adds `HealthIndicator.test.tsx` covering `healthStatus()` for each
  index state, and requires `npm run build` (`tsc -b && vite build`) to pass.

## R12. Integration tests (real services, opt-in)

**Decision**: These live in `tests/integration/test_index_stamp_milvus.py` and
`tests/integration/test_index_stamp_neo4j.py`, both marked `slow` and skipped unless
`MILVUS_TEST_URI` / `NEO4J_TEST_URI` is set.

- **Milvus**: a uniquely named collection (`itest_stamp_<hex>`). It checks the properties
  round-trip, reading the dimension from the schema, and adoption against real stored vectors
  with a fake embedder returning the stored vector. The collection is dropped afterwards.
- **Neo4j**: the meta node id is a module constant (`_META_ID = "index"`). The test patches it to
  `itest-stamp-<hex>` and deletes that node afterwards. It never touches a real deployment's
  stamp.
