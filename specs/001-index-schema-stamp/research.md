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
- The check runs in `lifecycle.startup()` in this order:
  1. `graph_store.ensure_schema()`;
  2. the embedding proxy is created, which legacy verification needs (`lifecycle.py:425-439`);
  3. `JobStore().init()` and `JobGroupStore().init()`, which the check needs to read the shared
     rebuild state (R13);
  4. **the check itself**;
  5. `PostgresJobQueue().start()` (`:450-457`);
  6. recovered jobs are re-enqueued (`:460-512`).

  The check therefore runs after the stores it reads are ready, and no worker in this process
  can pick up a job before the status is known.
- The check's own I/O is bounded: store reads use the adapters' normal timeouts, and legacy
  re-embedding is capped by `INDEX_VERIFY_TIMEOUT_SECONDS` (default 15). If the check cannot
  complete because a store or the embedding service is unreachable, the status is `unverified`
  with a reason naming what could not be reached. The refresh loop (R4) then retries it (FR-011), and
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
   - If none of the 20 rows is short enough, inspect up to 200 rows, the same way. If there is
     still none, the result is `failed("no verifiable chunks", …)`, so the status is
     `reindex_required`. Data is never adopted without a verified sample (FR-004).
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

## R4. Where the index status lives; the refresh loop

**Decision**

- **Pure logic in `src/treeweft/domain/index_stamp.py`** (no adapter imports):
  - `IndexStamp`, `ConfiguredIndex`, `StoreCheck`, `RebuildState`, `IndexStatus`;
  - `decide_store(...)`: the ADR-004 §3 decision table per store;
  - `aggregate(...)`: worst-of across stores and the rebuild state (precedence in data-model).
- **Runtime state and orchestration in `src/treeweft/application/index_guard.py`**:
  - a per-process **cached** `IndexStatus`, held in a module global;
  - `run_check()`, `refresh()`, the refresh loop;
  - `require_searchable()` / `require_writable()` (read the cache);
  - `dispatch_allowed(job)` (authoritative; reads Postgres, see R13);
  - `rebuild()`.
- **The shared truth lives outside the process**:
  - the stamps are in the stores;
  - the rebuild group and its jobs are in Postgres;
  - the maintenance lock is a Postgres advisory lock (R13).

  Each process only caches a view of that truth. This is what makes several indexer processes
  work (R13).
- **Refresh loop**: one asyncio task per process, started in `lifecycle.startup()` after the
  first check and cancelled in `shutdown()`, like the existing background loops
  (`lifecycle.py:516-545`). It calls `refresh()` every `INDEX_STATUS_REFRESH_SECONDS`
  (default 5). `refresh()`:
  1. Reads the rebuild state from Postgres. This is always done, and is cheap: one group row,
     its job statuses, and one `pg_locks` probe.
  2. Re-observes the store stamps without embedding whenever the cached status is not `ok` or
     the rebuild state changed since the last refresh. This lets a process that saw
     `reindex_required` notice that another process rebuilt the index.
  3. Retries legacy verification (with embedding) while the status is `unverified`, at most
     every `INDEX_VERIFY_INTERVAL_SECONDS` (default 60).
  4. Publishes the aggregated status to the cache.
- **Inline re-check (FR-011)**: when the status is `unverified`, `require_writable()` also runs
  `run_check()` once inline before refusing a job.
- **Serialisation**: an `asyncio.Lock` serialises checks within a process.
- **Staleness bound**: `/health` and the route gates read only the cache, so they never touch a
  store or Postgres. That resolves analysis finding I1. Across processes, a view is stale for at
  most `INDEX_STATUS_REFRESH_SECONDS`. Anything that must not be stale (a job about to write)
  uses `dispatch_allowed()` instead.

**Rationale**:

- Keeping the decision table in `domain/` makes it unit-testable with plain values, which is
  constitution IV layering.
- Caching keeps request paths cheap. The authoritative check is paid once per job, which is
  negligible next to indexing work.
- `wiring.py` is dead code (it imports classes that no longer exist) and is not a model to
  follow.

**Alternatives considered**:

- A status row in Postgres. Rejected: the stamp check has to describe the stores as they are,
  and every process can observe them directly. Postgres holds only what is genuinely shared
  coordination: the rebuild group and the lock.
- Checking Postgres on every request. Rejected: it puts a database round trip on every search.

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
2. **Execution choke point (authoritative, cross-process).** The queue worker
   (`adapters/queue/postgres_queue.py` `_run_one`) does two things, in this order:
   1. **Persists the job as `running`**, and waits for that write to commit.
   2. **Calls `await index_guard.dispatch_allowed(job)`**. This reads the shared state (R13)
      rather than the cache. The rules are applied in order:
      1. **A rebuild-group job is always allowed.** If the job's `group_id` is the latest
         `index-rebuild` group and that group is not `interrupted`, allow it. The rebuilder
         stamped the stores before enqueueing it (R7 steps 4–5), so no process's cached status
         can be a reason to refuse it.
      2. **Refuse** if the maintenance lock is held exclusively (a rebuild is
         preparing).
      3. **Refuse** if the latest rebuild was `interrupted`.
      4. If this process's **cached** status is `reindex_required` or `unverified`, do not trust
         it alone. **Re-observe the stamps directly** (no embedding, under the check lock) and
         refuse only if that fresh check still says `reindex_required` or `unverified`. A cache
         that is up to 5 s stale just after a rebuild therefore never fails a legitimate job.
         The fresh result also updates the cache.
      5. Otherwise allow.

   A refused job is marked `failed` with the reason, the same path an undispatchable job takes
   today (`:132-140`). Attempts are not incremented, so the job is not retried. The
   running-before-check order is what makes the rebuild safe across processes (R13). This covers every path the HTTP
   layer cannot see: fleet auto-refresh (`_enqueue_source_reindex`), restart recovery, and jobs
   queued before a status change.
3. **A route-table test.** It asserts that every route in the read set calls
   `require_searchable` and every route in the write set calls `require_writable`, by inspecting
   route endpoint source. Any new route under `/index-*`, `/find-*`, `/search*` or `/graph-*`
   must appear in one of the sets or in an explicit exempt list. This keeps the gate from
   silently missing a future endpoint (constitution V).

**Rebuild jobs are allowed**:

- `writes_allowed()` (the cached route gate) is true while the status is `ok`, or `rebuilding`
  once the stores are recreated. It is false while `reindex_required` or `unverified`, and while
  `preparing`.
- The rebuild's own jobs never pass through the routes. They are enqueued internally in R7 step
  5, and can be popped by any process while the lock is still held. `dispatch_allowed()` lets
  them through because their `group_id` is the group being prepared (R13).
- Every other job is refused at dispatch until the lock is released.

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

**Decision**: `POST /index/rebuild` (admin only, `authz._require_admin`), `?dry_run=true`. It
requires Postgres; without a pool it returns 503 "rebuild requires the job database".

- **Dry run** returns each source from `source_records` (`id`, `label`, `kind`, `chunk_count`),
  the totals, and the blockers that would make the real call fail right now. It writes nothing
  and takes no lock.
- **The real call, in order**: each step's effects are committed before the next step starts.
  - **Step 0. Fast pre-check.** Collect the blockers:
    - jobs `queued` or `running` in any process (`JobStore.list_by_status`, global);
    - the maintenance lock held in any mode;
    - a rebuild in progress: the lock is held exclusively, or jobs of the latest rebuild group
      are still `queued` or `running`.

    An `interrupted` group is **not** a blocker. Rebuilding is how an interrupted rebuild is
    recovered, and the new group becomes the latest one.

    If there are any, return 409 listing them. Nothing has been written.
  - **Step 1. Take the maintenance lock exclusively** on a dedicated connection (R13). If that
    fails, return 409 "another rebuild or community build is in progress". From here on, every
    process's `dispatch_allowed()` refuses jobs that are not part of this rebuild.
  - **Step 2. Create the job group**, with `kind="index-rebuild"` and `task_count` preset to the
    number of sources (not incremented per attach).
  - **Step 3. Re-check the blockers** (queued and running jobs). A job that another process
    marked `running` before step 1 is visible now (R13). If there are any, delete the group,
    release the lock, and return 409. Nothing has been dropped.
  - **Step 4. Recreate the stores.**
    - Drop the vector collection, then run `init_collection()`, which stamps it (R2).
    - Clear the graph index data. On Neo4j this is batched
      `MATCH (n) WHERE NOT n:TreeweftMeta CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF
      10000 ROWS` in an auto-commit session. On SQLite it is `clear_all()`, which already spares
      `treeweft_meta`.
    - Stamp the graph, then call `retrieval.invalidate_graph_caches()` in this process. Other
      processes invalidate theirs when `refresh()` sees the new rebuild group.
  - **Step 5. Enqueue the jobs.** For each source, build the job exactly as
    `_enqueue_source_reindex` does (same kind resolution), set `group_id`, persist it and
    enqueue it. Workers in any process may start these at once: `dispatch_allowed()` lets jobs
    of the group being prepared through.
  - **Step 6. Release the lock** by closing the dedicated connection. This process's status is
    refreshed immediately, and the others follow within `INDEX_STATUS_REFRESH_SECONDS`.
- **If this process fails after step 2**:
  - its connection closes and the lock is released;
  - the group is left with fewer jobs than `task_count`, and eventually none of them is active;
  - every process then derives `interrupted`, which means `reindex_required` ("a rebuild was
    interrupted; run it again");
  - a step that raises inside the handler also returns 503 naming the step.
- **Graph clear scope**: the whole graph except the meta node, including `Source` nodes. The job
  runners recreate them by `MERGE`. The fleet list reads `source_records` first; the graph
  `Source` fallback is only used when Postgres is down.
- **Rebuild state** is derived in `refresh()` from the latest `index-rebuild` group and the lock
  probe (data-model "RebuildState"):
  - lock held exclusively → `preparing`, reported as `rebuilding` with done 0;
  - some jobs of the group queued or running → `rebuilding`, with
    `rebuild_progress = {"done": done+failed+dead_letter, "total": task_count}`;
  - fewer jobs than `task_count`, none active, and the lock not held → `interrupted`;
  - otherwise `complete`, and the stamp check alone decides. A group with `task_count == 0` is
    complete immediately.
- **No `force` flag needed**: the "already indexed, skipping" check exists only in the HTTP
  routes (`indexer_service.py:796, 831, 888`). The runners' resume list comes from the vector
  store (`list_indexed_paths`, `indexer_runners.py:646-654`), which is empty after the drop.
- **Kept**: the summary cache (Postgres `summary_cache`, keyed `(sha1, model, prompt_version)`,
  migration 005), `source_records`, users, groups, keys and job history.

**Alternatives considered**:

- Enqueueing the jobs before dropping the stores. Rejected: a worker could write into the old
  collection before the drop.
- A new Postgres table for rebuild state. Rejected: `job_groups.kind`, a preset `task_count` and
  an advisory lock carry everything, with no migration.
- A time-based "preparing" grace period instead of a lock. Rejected: any timeout is either too
  short for a large drop or too long to notice a crash. The lock is released exactly when its
  holder dies.

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
- **Neo4j**:
  - The meta node id is a module constant (`_META_ID = "index"`). The test patches it to
    `itest-stamp-<hex>` and deletes that node afterwards, so it never touches a real
    deployment's stamp.
  - The clear is tested through an internal `_clear_index_data(scope_prefix: str | None)`. The
    public `clear_index_data()` calls it with `None`. The test calls it with its run prefix,
    which adds `AND n.id STARTS WITH $prefix`, so only the test's own nodes are deleted. An
    unscoped clear is never run against a shared database (constitution II; analysis finding
    C1).

## R13. Several indexer processes

**Constraint**: several indexer processes sharing one Postgres, one job queue and one set of
stores is a supported topology (`docs/engineering-notes.md:297`), and this feature MUST work in it
(maintainer decision, 2026-09-25).

**Decision**

- **Shared truth, per-process cache.**
  - The stamps live in the stores; the rebuild group and its jobs live in Postgres.
  - One Postgres **session-level advisory lock**, the *maintenance lock*, is the only
    cross-process coordination. It is taken with `pg_try_advisory_lock(k1, k2)` using a fixed
    two-integer key: `k1 = hashtext('treeweft')`, `k2 = hashtext('index-maintenance')`.
  - Every process caches its view and refreshes it (R4).
- **Who takes the lock, and in what mode**:

  | Holder | Mode | Held for |
  |---|---|---|
  | Rebuild | exclusive | steps 1–6 of R7 |
  | `POST /build-community` backfill | shared (`pg_try_advisory_lock_shared`) | its whole run |
  | Stamp writes by `run_check()` (fresh stamp or adoption) | shared, for the write only | the write only |

  - A process that cannot get the shared lock skips its stamp write and reports `preparing`
    (another process is rebuilding). It never stamps over a rebuild in progress.
  - Community build refuses with 409 while the lock is held exclusively.
  - The rebuild refuses with 409 while any shared holder exists.
- **The lock is held on a dedicated `asyncpg.connect()` connection, not taken from the pool.**
  The pool's `max_size` is 5 (`adapters/postgresql/connection.py`), too small to pin
  connections for minutes. When the holder process dies, its connection closes and Postgres
  releases the lock. That is how a crash turns into `interrupted` with no timeout.
- **Lock probe**: other processes probe the lock without taking it:
  `SELECT mode FROM pg_locks WHERE locktype = 'advisory' AND classid = $1 AND objid = $2 AND
  granted`. Only `refresh()` and `dispatch_allowed()` run this probe; request paths never do.
- **Why no job can write into stores being dropped** (the interleaving argument for analysis
  finding U2). Let rebuild process A and worker process B run concurrently. The two orders are:
  - **A**: take the lock (step 1), then create the group (step 2), then read the running and
    queued jobs (step 3).
  - **B**: persist its job as `running`, then read the lock and group state in
    `dispatch_allowed()`.

  Each side's write commits before it reads. If B's read comes before A's step 1, then B's
  `running` write committed before A's step 3 read, so A sees it and aborts before dropping
  anything. Otherwise B's read sees the lock held and refuses the job. Either way, no job
  outside the rebuild group runs while stores are being dropped.
- **Rebuild-group jobs versus stale caches**: `dispatch_allowed()` allows a job of the latest,
  non-interrupted rebuild group before it looks at any cached status (R5 §2, rule 1). The cached
  `reindex_required` is almost always stale exactly then, both in other processes and in the
  rebuilder's own process until its immediate refresh at step 6, and it must not fail the
  rebuild. Non-rebuild jobs re-observe the stamps before being refused (rule 4).
- **Stale caches** (at most `INDEX_STATUS_REFRESH_SECONDS`) can therefore only cause a request to
  be refused or accepted slightly late. They can never cause a write into an inconsistent
  index:
  - a job enqueued through a stale cache is refused at dispatch;
  - a search in a process whose cache still says `ok` may reach a store while another process
    is dropping it. The read routes handle that through `index_guard.store_error_response(exc)`:
    when a store call raises, it probes the lock, and if the lock is held exclusively it returns
    the `preparing` 409. Otherwise the error propagates as today. The probe runs only on this
    error path, so normal searches pay nothing. A reader therefore sees a `preparing` 409 or
    partial results, never a store error caused by a rebuild.
- **No Postgres configured** (`DATABASE_URL` unset or the pool unavailable, the degraded mode
  `connection.py` allows):
  - Cross-process coordination is impossible, and not needed: the job queue itself requires
    Postgres, so only one process can be doing index work.
  - `acquire()` returns a **no-op handle** (`coordinated = False`) and logs a WARNING once. Stamp
    writes and the community build proceed normally.
  - `probe()` returns None, so `dispatch_allowed()` relies on the cached stamp state alone.
  - The rebuild returns 503, because it needs job groups.
  - `acquire()` returns None **only** when Postgres is present and another process holds the
    lock in a conflicting mode.
- **Startup in a new process during a rebuild**: the first `run_check()` probes the lock, sees
  `preparing`, and writes no stamp.
- **Two processes adopting at the same moment**: both write the same stamp under the shared
  lock. The writes are idempotent.

**Tests**:

- Unit, with a fake lock and a fake job store that let the test interleave steps:
  - both orders of the A/B argument above;
  - community build versus rebuild in each order;
  - a crash (the lock is released and the group is incomplete) leading to `interrupted`;
  - a stale cache in B leading to a job refused at dispatch;
  - B's `refresh()` moving from `reindex_required` to `rebuilding` to `ok` after A's rebuild.
- Integration (opt-in, real Postgres at `POSTGRES_TEST_URL`, `slow`): two real connections
  prove that the probe query sees an exclusive advisory lock taken by the other connection, and
  that closing the holder's connection releases it.

**Alternatives considered**:

- Pinning to one indexer process. Rejected by the maintainer: several processes are required.
- Row-level locking on a "maintenance" table. Rejected: it needs a migration and still has to
  handle crashed holders; an advisory lock is released by Postgres itself.
