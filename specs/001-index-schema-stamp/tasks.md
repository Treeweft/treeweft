---
description: "Task list for 001-index-schema-stamp (ADR-004 §3)"
---

# Tasks: Index Schema Stamp, Reindex-Required Mode and Rebuild

**Input**: Design documents from `specs/001-index-schema-stamp/`: [plan.md](plan.md),
[spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md),
[contracts/](contracts/), [quickstart.md](quickstart.md).

**Tests**: REQUIRED. Constitution II requires test-first for every behaviour change, and ADR-004 §6
lists the tests. In each phase the test tasks come first. They MUST fail before the implementation
task that follows them is started.

**Test command**: `env -u PYTHONPATH python -m pytest tests/unit -q`. Unit tests never touch
Milvus, Neo4j, Postgres or a model server. Mock those at the client boundary. Embedded LanceDB,
Chroma and SQLite on `tmp_path` are allowed.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US4 from spec.md

---

## Phase 1: Setup

**Purpose**: Constants and configuration that everything else reads.

- [ ] T001 Add `INDEX_SCHEMA_VERSION = 1` to `src/treeweft/versions.py`, with a docstring
  line citing ADR-004 §3: "Bumping it requires a SemVer MAJOR bump; summary-prompt changes never
  bump it". Test in `tests/unit/test_versions.py`: the value is an `int` ≥ 1.
- [ ] T002 [P] Add `INDEX_VERIFY_INTERVAL_SECONDS` (default 60) and
  `INDEX_VERIFY_TIMEOUT_SECONDS` (default 15) to `src/treeweft/infrastructure/config.py`:
  - Both are positive numbers.
  - An invalid value fails `validate_config()` with a message naming the setting (constitution V).
  - Add commented defaults to `.env.example`.
  - Test in `tests/unit/test_config.py`: defaults, an override, and invalid values rejected.

---

## Phase 2: Foundational (blocking prerequisites)

**Purpose**: The pure decision logic and the store functions behind the shims
([contracts/store-ports.md](contracts/store-ports.md)). Every story needs them.

**⚠️ No user-story work starts until this phase is complete.**

### Decision logic

- [ ] T003 [P] Write `tests/unit/test_index_stamp_decision.py`. Cover every row of the
  `decide_store` table in [data-model.md](data-model.md) as a parametrized case:
  - unreachable → `unverified`;
  - vector collection absent → `ok`, no stamp;
  - no data and no stamp → `ok` plus write stamp;
  - matching stamp → `ok`;
  - each of `schema`/`embedding_model`/`vector_dim` differing → `reindex_required`;
  - stored schema > current → `reindex_required`;
  - unreadable stamp → `reindex_required` (never treated as "no stamp");
  - legacy `passed`/`failed`/`unavailable`/`not_run` (`not_run` → `unverified`, no stamp);
  - graph data with no stamp while the vector store has no data → `reindex_required`.

  Also assert:
  - the exact reason format `"<role> store (<backend>): <field> is <stored>, configured <configured>"`,
    with several reasons joined by `"; "`;
  - `aggregate` precedence `reindex_required` > `unverified` > `rebuilding` > `ok`.
- [ ] T004 Implement `src/treeweft/domain/index_stamp.py`: `IndexStamp(schema: int,
  embedding_model: str, vector_dim: int)`, `ConfiguredIndex`, `StoreObservation(store, backend,
  exists, has_data, stamp, schema_dim, unreachable)`, `Verification`
  (passed | failed(check, detail) | unavailable(detail) | not_run), `StoreCheck`,
  `IndexStatus(state, reason, rebuild_progress)`, `decide_store`, `aggregate`, `format_reason`,
  `parse_stamp`.
  - `parse_stamp` turns str→str maps into an `IndexStamp`, or an "unreadable" marker.
  - The module is pure: no imports from `treeweft.adapters`, `treeweft.application` or
    `treeweft.infrastructure`.
  - T003 passes.

### Store functions (one pair per backend; the pairs are parallel to each other)

- [ ] T005 [P] Write `tests/unit/test_sqlite_index_stamp.py`, using the existing autouse fixture
  pattern from `tests/unit/test_sqlite_graph_store.py:14-21` (`GRAPH_DB_PATH` →
  `tmp_path/graph.db`). Cover:
  - `ensure_schema` creates `treeweft_meta`;
  - the `write_stamp`/`read_stamp` round-trip, and a second write upserts rather than duplicating;
  - `observe_index().has_data` is false on empty and true after one entity is inserted;
  - `clear_index_data()` and `clear_all()` remove entities and communities but keep the stamp;
  - a row with a non-integer `index_schema` makes `observe_index` report an unreadable stamp.
- [ ] T006 [P] Implement in `src/treeweft/adapters/sqlite/graph_store.py`:
  - add to `_SCHEMA_STATEMENTS`: `CREATE TABLE IF NOT EXISTS treeweft_meta (id TEXT PRIMARY KEY
    CHECK (id = 'index'), index_schema INTEGER NOT NULL, embedding_model TEXT NOT NULL,
    vector_dim INTEGER NOT NULL, stamped_at INTEGER NOT NULL)`;
  - add `read_stamp`, `write_stamp` (`INSERT … ON CONFLICT(id) DO UPDATE`), `observe_index`
    (`SELECT 1 FROM entities LIMIT 1`) and `clear_index_data` (delegates to `clear_all`, which
    already spares `treeweft_meta`).

  T005 passes.
- [ ] T007 [P] Write `tests/unit/test_neo4j_index_stamp.py`, with the Neo4j driver mocked at the
  session boundary in the same way as `tests/unit/test_graph_store_adapter.py:48`. Assert:
  - `_SCHEMA_STATEMENTS` includes `CREATE CONSTRAINT treeweft_meta_id_unique IF NOT EXISTS FOR
    (m:TreeweftMeta) REQUIRE m.id IS UNIQUE`;
  - `write_stamp` issues `MERGE (m:TreeweftMeta {id: $id}) SET …` with `_META_ID`;
  - `read_stamp` returns None when there is no row;
  - `observe_index` uses `MATCH (e:Entity) RETURN 1 LIMIT 1` with `fetch(1)` (integer argument,
    as the driver 6.x invariant requires);
  - `clear_index_data` and `clear_all` both contain `WHERE NOT n:TreeweftMeta`, and
    `clear_index_data` is batched with `IN TRANSACTIONS OF 10000 ROWS` in an auto-commit
    `session.run`;
  - a driver error → `observe_index().unreachable` is set, and nothing is raised.
- [ ] T008 [P] Implement in `src/treeweft/adapters/neo4j/graph_store.py`:
  - the module constant `_META_ID = "index"` and the new constraint;
  - `read_stamp`, `write_stamp`, `observe_index`, and `clear_index_data`
    (`MATCH (n) WHERE NOT n:TreeweftMeta CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF
    10000 ROWS`);
  - change `clear_all()` to spare `:TreeweftMeta`;
  - wrap the new functions in `@_retry_on_disconnect`, like the existing ones.

  T007 passes.
- [ ] T009 [P] Write `tests/unit/test_lancedb_index_stamp.py`, with a real embedded LanceDB in
  `tmp_path` and module attributes monkeypatched as in `tests/unit/test_lancedb_adapter.py:16-22`.
  Cover:
  - a table that does not exist → `observe_index().exists is False`;
  - `init_collection()` on a fresh path creates the table **and** stamps the `vector` field
    metadata;
  - the `write_stamp` round-trip merges with existing field metadata (plant an unrelated key and
    assert it survives);
  - the stamp survives an insert, a delete, FTS index creation, `optimize()` and reopening;
  - `schema_dim` comes from `list_size`;
  - `sample_chunks(3)` returns `(text, vector)` pairs and skips rows with
    `len(chunk_text) >= 50000`;
  - `drop_index()` removes the table.
- [ ] T010 [P] Implement in `src/treeweft/adapters/lancedb/vector_store.py`:
  - `_schema(dim)` takes the dimension, and the module constant `FTS_COLUMN = "chunk_text"` is used
    by `_get_table`;
  - the stamp lives in `tbl.replace_field_metadata("vector", merged)` under the keys
    `treeweft.index_schema` and `treeweft.embedding_model`, read back from
    `tbl.schema.field("vector").metadata` (bytes);
  - add `observe_index`, `write_stamp`, `sample_chunks` and `drop_index`;
  - table creation stamps the new table.

  T009 passes.
- [ ] T011 [P] Write `tests/unit/test_chromadb_index_stamp.py`, with a real embedded Chroma
  client in `tmp_path`. Cover:
  - `init_collection()` stamps a new collection's metadata (`treeweft.index_schema`,
    `treeweft.embedding_model`, `treeweft.vector_dim`);
  - `write_stamp` keeps existing metadata keys, including any `hnsw:*`. Pin down whether
    `collection.modify(metadata=)` merges or replaces in chromadb 1.5.9, and implement against
    the observed behaviour;
  - `schema_dim` comes from a stored embedding's length;
  - `sample_chunks` and `drop_index` work.
- [ ] T012 [P] Implement in `src/treeweft/adapters/chromadb/vector_store.py`:
  - module-level `observe_index`, `write_stamp`, `sample_chunks` and `drop_index`, in the same
    style as the existing module-level exports that `src/treeweft/retriever.py:26-31` imports;
  - `init_collection` passes the stamp in `get_or_create_collection(metadata=…)` when it
    creates the collection.

  T011 passes.
- [ ] T013 [P] Write `tests/unit/test_milvus_index_stamp.py` with `MilvusClient` mocked (Milvus
  is external). Assert:
  - `init_collection()` on a missing collection calls `create_collection(…, properties={
    "treeweft.index_schema": "1", "treeweft.embedding_model": <EMBEDDING_MODEL>})`;
  - `observe_index` reads `describe_collection()["properties"]` (str values, schema parsed with
    `int`) and the dimension from the `vector` field's `params.dim`;
  - `has_data` comes from `get_collection_stats()["row_count"] > 0`;
  - `write_stamp` calls `alter_collection_properties`;
  - `sample_chunks` calls `query(filter="", output_fields=["chunk_text","vector"], limit=20)` and
    skips long rows;
  - `drop_index` calls `drop_collection`;
  - a client error → `unreachable` is set.
- [ ] T014 [P] Implement in `src/treeweft/adapters/milvus/vector_store.py`:
  - extract `build_collection_schema(dim)` and `INDEX_PARAMS` to module level (research R9);
    `init_collection` uses them, and the schema stays identical;
  - add `observe_index`, `write_stamp`, `sample_chunks` and `drop_index` on `MilvusAdapter`,
    plus module-level wrappers over `_get_adapter()`.

  T013 passes, and the existing `tests/unit/test_milvus_adapter.py` still passes.

### Shims

- [ ] T015 Write `tests/unit/test_store_shim_exports.py`:
  - for each graph backend module, every name in `graph_store._EXPORTED` is defined;
  - for each vector backend, `observe_index`, `write_stamp`, `sample_chunks` and `drop_index` are
    reachable through `treeweft.retriever` under that `VECTOR_STORE`, using the subprocess-per-env
    pattern of `tests/unit/test_simple_profile.py`.

  Then add the names to `src/treeweft/graph_store.py` `_EXPORTED` (`observe_index`,
  `read_stamp`, `write_stamp`, `clear_index_data`) and to the three branches of
  `src/treeweft/retriever.py`. Confirm that `tests/unit/test_simple_profile.py` still passes.

**Checkpoint**: every store can observe, stamp, sample and drop, all behind the shims.

---

## Phase 3: User Story 1 — A mismatched index is detected and reported, never searched silently (P1) 🎯 MVP

**Goal**: a startup stamp check, the `/health` fields, 409s on read routes, refused index jobs,
MCP passthrough, and the UI indicator.

**Independent Test**: with fake stores, stamp an index with model A and start with model B
configured. `/health` shows `reindex_required` naming the store, field and both values. The read
routes return 409 per [contracts/errors.md](contracts/errors.md). Admin, auth and source routes
still work.

### Tests for User Story 1 (write first, confirm they fail)

- [ ] T016 [P] [US1] Write `tests/unit/test_index_guard.py` (check and health part), with fake
  vector and graph shims injected by monkeypatching `treeweft.retriever` and
  `treeweft.graph_store` attributes. Cover:
  - fresh stores → graph stamped, `ok`;
  - matching stamps → `ok`, nothing written;
  - model mismatch → `reindex_required` with the exact reason;
  - both stores mismatched → both reasons;
  - `GET /health` (via `TestClient`) contains `index_schema: 1`, `index_status`, and
    `reindex_reason` only when relevant;
  - `status == "ok"` in every state;
  - `/health` makes no store call (the fakes count calls).
- [ ] T017 [P] [US1] Write `tests/unit/test_index_gate_routes.py`. With the status forced to
  `reindex_required`:
  - `POST /search`, `POST /hydrate-chunks`, `GET /find-definition`, `GET /find-callers`,
    `GET /find-references` and `POST /graph-explore` return 409. The body has the
    `detail`/`reason`/`index_status`/`rebuild` keys, `len(detail) <= 300`, and `detail` still
    contains `/index/rebuild` when the reason is 1 000 characters long.
  - `POST /index-file`, `/index-directory`, `/index-repo`, `/index-graph`, `/jobs/{id}/retry`,
    `POST /build-community` and the job-enqueuing webhook routes return 409, and nothing is
    enqueued.
  - `/health`, `/sources`, `/jobs`, `/job-groups` and the auth routes answer normally.
  - With auth on and no credentials, gated routes return 401/403, not 409.
  - Route classification: every app route whose path starts with `/search`, `/hydrate`,
    `/find-`, `/graph-`, `/index-`, `/build-community` or `/jobs/{job_id}/retry` is in the read
    set, the write set, or an explicit exempt list with a comment. An unclassified new route
    fails the test.
- [ ] T018 [P] [US1] Write `tests/unit/test_index_dispatch_gate.py`: with writes refused, a job
  popped by `PostgresJobQueue._run_one` (job store faked) is persisted as `failed` with the
  errors.md `detail` as `error`. `dispatch_job` is never called, and `increment_attempts` is
  never called.
- [ ] T019 [P] [US1] Add to `tests/unit/test_mcp_compat.py`: a mocked indexer returning the
  errors.md 409 body to `search_code` gives `{"error": "Indexer returned HTTP 409: Index requires
  rebuild: …"}` containing `/index/rebuild`. The word "unreachable" never appears.
- [ ] T020 [P] [US1] Write `ui/src/components/layout/HealthIndicator.test.tsx` (vitest) for
  `healthStatus()` and the label:
  - `ok` → healthy;
  - `reindex_required` → error, "Re-index required", with the reason as the title;
  - `unverified` → warning, "Index unverified";
  - `rebuilding` with `{done: 3, total: 10}` → "Rebuilding 3/10";
  - a missing `index_status` (older indexer) → today's behaviour.

### Implementation for User Story 1

- [ ] T021 [US1] Implement `src/treeweft/application/index_guard.py`:
  - a module-level `_status: IndexStatus` and an `asyncio.Lock`;
  - `configured()` from `EMBEDDING_MODEL`, `VECTOR_DIM` and `versions.INDEX_SCHEMA_VERSION`;
  - `async run_check()`: observe both stores via the shims, `decide_store` each (legacy
    verification is `not_run` until T031), perform the stamp writes the decisions require, then
    `aggregate`. Log the result.
  - `status()`, `writes_allowed()` (true only for `ok` and `rebuilding`),
    `require_searchable()` and `require_writable()`, which raise `HTTPException(409, …)` through
    a `JSONResponse` builder that produces the [contracts/errors.md](contracts/errors.md) body,
    truncating the reason inside `detail` so it stays ≤ 300 characters with the pointer kept.
  - `health_fields()` returns the `/health` additions.
  - Imports stores only through `treeweft.retriever` and `treeweft.graph_store`.

  T016 passes for its non-legacy cases.
- [ ] T022 [US1] In `src/treeweft/application/lifecycle.py` `startup()`, call
  `await index_guard.run_check()` after `graph_store.ensure_schema()` and the embedding-proxy
  setup (`:413-439`), and **before** `JobStore().init()` / `PostgresJobQueue().start()` and job
  recovery (`:450-512`). Test in `tests/unit/test_lifecycle_order.py` (or the existing lifecycle
  test): a recording fake shows the check runs before the queue starts and before any recovered
  job is enqueued.
- [ ] T023 [US1] Merge `index_guard.health_fields()` into `GET /health` in
  `src/treeweft/application/indexer_service.py:497-513`. Existing fields are unchanged.
- [ ] T024 [US1] Call `index_guard.require_searchable()` in the six read routes in
  `src/treeweft/application/indexer_service.py` (`/search` 1649, `/hydrate-chunks` 1698,
  `/find-definition` 1714, `/find-callers` 1735, `/find-references` 1761, `/graph-explore` 1790),
  immediately **after** each route's existing `authz._authorize_scope` call.
- [ ] T025 [US1] Call `index_guard.require_writable()` after authorization in
  `/index-file` 776, `/index-directory` 814, `/index-repo` 866, `/index-graph` 1283,
  `/jobs/{id}/retry` 729 and `POST /build-community` 1432 in
  `src/treeweft/application/indexer_service.py`, and before each enqueue in
  `src/treeweft/application/routes_webhook.py` (`:263`, `:316`, `:339`). T017 passes.
- [ ] T026 [US1] Gate `src/treeweft/adapters/queue/postgres_queue.py` `_run_one`: before
  `dispatch_job`, `if not index_guard.writes_allowed()`, persist the job as `failed` with
  `error = index_guard.refusal_detail()` and return. Use the lazy import already used there, and
  do not increment attempts. T018 passes.
- [ ] T027 [P] [US1] Extend `ui/src/components/layout/HealthIndicator.tsx`: `HealthResponse` gains
  `index_status?`, `reindex_reason?` and `rebuild_progress?`, and `healthStatus()` and the label
  follow T020. T020 passes (`cd ui && npm test`), and `npm run build` type-checks.
- [ ] T028 [US1] Regression proof (constitution II): temporarily remove the guard calls from
  T024–T026. Confirm T017 and T018 fail, restore the calls, and save the failing output for the
  PR description. T019 must already pass on the Plan 1 code, which confirms that FR-018 needs no
  MCP code change.

**Checkpoint**: US1 is complete. A mismatched index is detected, reported and gated.

---

## Phase 4: User Story 2 — Existing (pre-1.0.0) deployments are adopted without a forced re-index (P1)

**Goal**: legacy verification (dimension, then re-embedding up to 3 chunks at cosine ≥ 0.99),
`unverified` with an inline and background retry, and graph adoption tied to the vector result.

**Independent Test**: fake stores with data and no stamp, plus a fake embedder:

- vectors re-embed at ≥ 0.99 → adopted and stamped;
- they re-embed differently → `reindex_required` naming the check;
- the embedder is down → `unverified`, search allowed, jobs refused, no stamp;
- the embedder comes up → the next job submission or retry adopts the data, with no restart.

### Tests for User Story 2 (write first, confirm they fail)

- [ ] T029 [P] [US2] Write `tests/unit/test_legacy_verification.py`:
  - the dimension check fails before any embedding;
  - cosine 0.995 on all samples → passed, and 0.98 on one → failed with the values in the
    detail;
  - the fake embedder records that `embed()` was called and `embed_query()` never was;
  - long rows are never sampled;
  - 2 rows → verified with 2, and 0 rows → treated as "no data";
  - an `httpx.HTTPError`, a `RuntimeError("All embedding backends are unavailable …")` or a
    timeout beyond `INDEX_VERIFY_TIMEOUT_SECONDS` → unavailable;
  - an unstamped graph is adopted only when vector verification passed;
  - an unstamped graph with data and an empty vector store → `reindex_required`;
  - an INFO log line contains the per-sample cosines.
- [ ] T030 [P] [US2] Add to `tests/unit/test_index_guard.py` (the `unverified` part):
  - with the embedder down at startup: `/search` returns 200, `/index-repo` returns 409 with the
    "Index unverified" `detail`, and no stamp is written;
  - once the fake embedder recovers, the next `/index-repo` call re-runs verification inline,
    stamps the data, and returns 202;
  - the background loop (interval patched to 0.01 s) moves `unverified` → `ok`, or → `reindex_required`
    when verification then fails;
  - concurrent `require_writable()` calls run a single check (the lock);
  - the loop is idle when the status is `ok`.

### Implementation for User Story 2

- [ ] T031 [US2] Implement legacy verification in `src/treeweft/application/index_guard.py`:
  - `_verify_vector_store()` compares the dimension (`schema_dim` or the sample length) with
    `VECTOR_DIM`, then runs `sample_chunks(3)` and `embedder.embed(texts)` under
    `asyncio.wait_for(…, INDEX_VERIFY_TIMEOUT_SECONDS)`, then computes cosine ≥ 0.99 per sample;
  - it feeds the `Verification` into `decide_store` for the vector store, and then for the graph
    store, following the data-model rules;
  - it logs the cosines;
  - embedding goes only through the `treeweft.embedder` shim.

  T029 passes.
- [ ] T032 [US2] Add the retry to `src/treeweft/application/index_guard.py`:
  - `require_writable()` runs `run_check()` once inline when the status is `unverified`, then
    decides;
  - `start_retry_loop()` / `stop_retry_loop()` run `run_check()` every
    `INDEX_VERIFY_INTERVAL_SECONDS` while `unverified`;
  - wire start and stop into `src/treeweft/application/lifecycle.py` next to the existing
    background loops (`:516-545`) and in `shutdown()`.

  T030 passes.

**Checkpoint**: US1 and US2 are complete. Every existing deployment either adopts its data or
reports why it can't.

---

## Phase 5: User Story 3 — An admin rebuilds the index deliberately and watches it recover (P2)

**Goal**: `POST /index/rebuild` with a dry run, preset job-group orchestration, `rebuilding` with
progress, and interrupted-rebuild detection ([contracts/http-api.md](contracts/http-api.md),
research R7).

**Independent Test**: with fakes, the dry run lists sources and chunk counts and changes nothing.
The real call recreates and stamps the stores and enqueues one group. `/health` shows
`rebuilding`, then `ok`. Non-admins are refused.

### Tests for User Story 3 (write first, confirm they fail)

- [ ] T033 [P] [US3] Write `tests/unit/test_index_rebuild.py`, with fake shims and fake
  `JobStore`/`JobGroupStore`/queue/source repo in `indexer_state`.

  Dry run:
  - returns `sources[{id,label,kind,chunk_count}]`, `total_sources`, `total_chunks`,
    `index_status` and `blockers`;
  - every fake records zero writes.

  Real call:
  - returns 202 with `group_id`;
  - the call order is group created (`kind="index-rebuild"`, `task_count=len(sources)`) →
    `drop_index` → `init_collection` → `clear_index_data` → graph `write_stamp` →
    `invalidate_graph_caches` → status `rebuilding` → one enqueued job per source, each with
    `group_id` set and the same kind resolution as `_enqueue_source_reindex`;
  - `increment_task_count` is never called;
  - the `summary_cache` fake is untouched.

  Refusals and failures:
  - a queued or running job, a community build running, or an incomplete rebuild group → 409
    listing the blockers, with nothing dropped;
  - a non-admin gets 403 and an unauthenticated caller 401 (auth on);
  - `drop_index` raising → 503, and the status is `reindex_required` with
    "rebuild failed at drop vector store".

  Progress and completion:
  - `/health` shows `rebuilding` with `rebuild_progress {done,total}` counting
    done+failed+dead_letter;
  - search is allowed while `rebuilding`;
  - once all jobs are terminal (including one failed) the status is `ok`;
  - a group with fewer jobs than `task_count` and none active (a simulated crash after the drop)
    → at the next `run_check()` the status is `reindex_required` "a rebuild was interrupted; run
    it again";
  - zero sources → an empty group, and the status is `ok` immediately.

  Logging: a WARNING with `event=index_rebuild`, `user`, `dry_run`, `sources` and `total_chunks`
  is emitted for both the dry run and the real call.
- [ ] T034 [P] [US3] Add `latest_by_kind(kind)` to `src/treeweft/adapters/postgresql/job_group_store.py`,
  with an asyncpg-mocked unit test in `tests/unit/test_job_group_store.py`: it returns the most
  recent group of that kind or None. `create(label, kind, created_by, task_count)` already
  accepts a preset `task_count` (`:42-50`), and the test asserts that it is stored as passed.

### Implementation for User Story 3

- [ ] T035 [US3] In `src/treeweft/application/indexer_service.py`, extract the job-building part
  of `_enqueue_source_reindex` (`:1358-1387`) into
  `_build_source_reindex_job(src, group_id=None) -> dict`. `_enqueue_source_reindex` uses it, and
  the behaviour of the existing fleet auto-refresh is unchanged: the existing fleet tests still
  pass.
- [ ] T036 [US3] Add rebuild-status derivation to `src/treeweft/application/index_guard.py`:
  - use `JobGroupStore.latest_by_kind("index-rebuild")` and `JobStore.list_by_group`, cached for
    5 s;
  - it yields `rebuilding` + progress, `interrupted`, or `complete`;
  - `aggregate` receives it, `run_check()` includes it at startup, and `health_fields()`
    refreshes it through the cache.
- [ ] T037 [US3] Implement `async rebuild(dry_run: bool, user) -> dict` in
  `src/treeweft/application/index_guard.py`:
  - it collects blockers (`JobStore.list_by_status` queued/running,
    `_state._community_build_state`, an incomplete rebuild group) and sources
    (`_state._source_repo.list_all()`);
  - the real path follows the research R7 order and logs each stage;
  - on a failure at any step it sets `reindex_required` with "rebuild failed at <step>" and
    raises for a 503.
- [ ] T038 [US3] Add `POST /index/rebuild` (`dry_run: bool = False`) to
  `src/treeweft/application/indexer_service.py`:
  - `authz._require_admin(request)` runs first;
  - the handler delegates to `index_guard.rebuild`;
  - it returns 200 for a dry run and 202 for a real call, 409 with blockers, and 503 on a step
    failure;
  - the route is exempt from `require_writable` (listed in T017's exempt list).

  Add the route to `tests/unit/test_route_table.py:96`. T033 passes.

**Checkpoint**: US1–US3 are complete. The index can be detected, gated and rebuilt through the API.

---

## Phase 6: User Story 4 — Index-schema changes are caught by CI and recorded at release (P3)

**Goal**: the `contracts/index_schema.json` baseline, the `index-breaking` classification, and
the snapshot script (research R9).

**Independent Test**: a synthetic index-schema change is classified index-breaking and names the
minimum schema integer and version. The committed snapshot matches the code.

### Tests for User Story 4 (write first, confirm they fail)

- [ ] T039 [P] [US4] Extend `tests/unit/test_contracts.py`:
  - a table of synthetic index changes (a Milvus field added, removed or retyped; an index param
    changed; a Neo4j constraint removed; a SQLite column added; the LanceDB schema changed), each
    → `Change(kind="index-breaking")`;
  - `index_version_error(recorded_schema=1, current_schema=1, released="1.0.0",
    current="1.1.0")` → an error naming "INDEX_SCHEMA_VERSION must exceed 1 and the major must
    exceed 1 (at least 2.0.0)";
  - schema 2 plus 2.0.0 → no error;
  - the real check: `contracts/index_schema.json` is loaded, the `surface` is diffed against
    `index_schema_surface()`, and the result is asserted empty (else it fails with each change
    and the minimums);
  - `index_schema_surface()` contains the literal `"<VECTOR_DIM>"` and no integer dimension from
    `.env`.

### Implementation for User Story 4

- [ ] T040 [US4] In `src/treeweft/infrastructure/contracts.py`:
  - allow the `Change.kind` value `"index-breaking"`;
  - add `index_schema_surface()`. It imports the Milvus, LanceDB, Neo4j and SQLite adapter
    modules **inside the function**, builds `build_collection_schema(probe_dim)` and
    `INDEX_PARAMS`, `_schema(probe_dim)` and `FTS_COLUMN`, the Chroma stamp keys, and the Neo4j
    and SQLite `_SCHEMA_STATEMENTS`, then replaces the probe dimension with `"<VECTOR_DIM>"`;
  - add `diff_index(old, new)`, `index_version_error(...)`, and an optional
    `extra: dict | None` on `write_snapshot` (so the file can record `index_schema`).

  T039 passes except for the real snapshot check.
- [ ] T041 [US4] Update `scripts/update_contracts.py` to also write `index_schema` with
  `extra={"index_schema": versions.INDEX_SCHEMA_VERSION}`.
- [ ] T042 [US4] Generate the baseline `contracts/index_schema.json` once, with
  `source_version: "1.0.0"` (the last release, **not** the working 1.1.0) and `index_schema: 1`,
  using `write_snapshot` directly. Do not run the full `update_contracts.py`: that would also
  regenerate `api.json` and `mcp_tools.json`, which is release-PR-only. Commit it. T039 passes
  in full. `git diff contracts/api.json contracts/mcp_tools.json` must be empty.

**Checkpoint**: all four stories are complete.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T043 Set `pyproject.toml` to `version = "1.1.0"` and run `uv lock`. The existing
  `test_contracts.py` API check then passes: the new endpoint is additive, and 1.1.0 is above
  1.0.0 at minor level. Add a `CHANGELOG.md` "Unreleased" entry:
  - **Added**: the index stamp, `index_schema`/`index_status` in `/health`, reindex-required mode,
    `POST /index/rebuild`, and the `INDEX_VERIFY_*` settings;
  - **Operator configuration**: the first start after upgrading verifies an existing index by
    re-embedding up to 3 chunks, which needs the embedding service up.
- [ ] T044 [P] Write `tests/integration/test_index_stamp_milvus.py` (`@pytest.mark.slow`, skipped
  unless `MILVUS_TEST_URI`). Use a unique collection `itest_stamp_<hex>`. Cover:
  - create with properties;
  - `describe_collection` properties round-trip (this proves Milvus 2.5.4 accepts `treeweft.*`
    keys);
  - `alter_collection_properties` on an existing collection;
  - the dimension read from the schema;
  - `sample_chunks` over real inserted rows;
  - dropping the collection in teardown.
- [ ] T045 [P] Write `tests/integration/test_index_stamp_neo4j.py` (`slow`, skipped unless
  `NEO4J_TEST_URI`). Patch `_META_ID` to `itest-stamp-<hex>`, reset `_driver`, and cover:
  - the stamp round-trip;
  - the constraint exists;
  - `clear_index_data` scoped to test-prefixed entities spares the meta node. Use a test-only
    prefix filter, never an unscoped clear on a shared database.

  Teardown deletes the test meta node and entities.
- [ ] T046 [P] Update `docs/upgrading.md`:
  - reading `index_schema`, `index_status`, `reindex_reason` and `rebuild_progress`;
  - what `unverified` means (the embedding service is down at first start; it retries by itself
    and jobs are refused until then);
  - `reindex_required` and its causes;
  - the rebuild: dry run first, the real call, watching progress, and what is kept (the summary
    cache and sources);
  - the known limit: TEI serving a different model under the same `EMBEDDING_MODEL` name is not
    detected once the index is stamped.
- [ ] T047 [P] Update `docs/engineering-notes.md` near `:77-81` and `:281`: index states, the
  gate on routes and dispatch, the rebuild, the `INDEX_VERIFY_*` settings, and the rule that new
  index routes must call the guard.
- [ ] T048 [P] Update the ADR status line and the note in `docs/adr-004-compatibility-versioning.md`
  to record §3 as implemented. Add to `docs/adr-003-prompt-versioning.md` a note that
  summary-prompt changes never bump `INDEX_SCHEMA_VERSION`.
- [ ] T049 [P] Update the Principle VII transition note in `.specify/memory/constitution.md` to
  say that the rules are in force as of 1.1.0 (SemVer release tooling since 1.0.0, the index
  stamp since 1.1.0). Bump the constitution version to 1.0.1 (PATCH) and set Last Amended to
  the commit date.
- [ ] T050 [P] Update `CLAUDE.md` Invariants:
  - add "new routes under `/search`, `/find-*`, `/graph-*`, `/index-*` must call
    `index_guard.require_searchable`/`require_writable`; `test_index_gate_routes.py` guards
    this";
  - correct the Milvus bullet: `summary_vector` is **not** nullable (zero-filled on insert,
    `adapters/milvus/vector_store.py:236`);
  - add "never delete `:TreeweftMeta` / `treeweft_meta`".
- [ ] T051 Run the full unit suite: `env -u PYTHONPATH python -m pytest tests/unit -q`. Then run
  `cd ui && npm test && npm run build`. Record the pass/fail counts.
- [ ] T052 Run the integration tests from quickstart.md §2 against the homelab Milvus and Neo4j.
  If they are unreachable, say so and ask; do not work around it. Record the result or the skip
  reason.
- [ ] T053 Run the live end-to-end check from quickstart.md §4, after verifying the served
  embedding model and reranker. Record each step's observed `/health` output and HTTP codes for
  the PR.

---

## Dependencies & Execution Order

### Phase dependencies

- Setup (T001–T002) → Foundational (T003–T015) → US1 → US2 → US3. US4 needs only Foundational
  plus T014/T010 (the schema builders), so it can run in parallel with US1–US3. Polish comes last.
- US2 extends `index_guard.py` from US1 (T021), so it runs after US1.
- US3 needs US1's guard and status (T021, T023) and US2's aggregate wiring. Run it after US2.

### Within phases

- T004 unblocks T005–T014, because they return domain types.
- The backend pairs (T005/T006, T007/T008, T009/T010, T011/T012, T013/T014) are independent of
  each other. T015 needs all of them.
- T021 comes before T022–T026. T024 and T025 edit the same file (`indexer_service.py`), so they
  are sequential. T026 and T027 are separate files.
- T035 comes before T037, and T036 before T037 before T038.
- T040 comes before T041 and T042.

### Parallel opportunities

```text
Foundational:  T005+T006 | T007+T008 | T009+T010 | T011+T012 | T013+T014   (five backend tracks)
US1 tests:     T016 | T017 | T018 | T019 | T020
US1 impl:      T026 | T027 alongside T024→T025
US4:           T039→T042 alongside US1–US3 once T010/T014 are merged
Polish:        T044 | T045 | T046 | T047 | T048 | T049 | T050
```

## Implementation Strategy

- **MVP**: Setup, Foundational and US1. Mismatches are detected and loudly gated. Without US2,
  legacy verification is `not_run`, so every existing unstamped deployment would show
  `unverified` and refuse index jobs. US1 and US2 are both P1 and ship together.
- **Increments**: US1+US2 (safe to deploy) → US3 (self-service recovery) → US4 (future-proofing)
  → Polish. A single PR is expected, per ADR-004 Plan 2. Commit after each task or task pair.
- **Verification**: T028 (regression proof), T051–T053 (quickstart), all reported in the PR
  under constitution I.
