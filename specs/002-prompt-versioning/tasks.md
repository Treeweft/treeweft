---
description: "Task list for 002-prompt-versioning (ADR-003)"
---

# Tasks: Prompt Versioning with Admin Pins and Summary-Only Refresh

**Input**: Design documents from `specs/002-prompt-versioning/`: [plan.md](plan.md),
[spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md),
[contracts/](contracts/), [quickstart.md](quickstart.md).

**Tests**: REQUIRED. Constitution II requires test-first for every behaviour change, and ADR-003
§5 lists the tests. In each phase the test tasks come first. They MUST fail before the
implementation task that follows them is started.

**Test command**: `env -u PYTHONPATH python -m pytest tests/unit -q`. Unit tests never touch
Milvus, Postgres or a model server. Postgres is faked with the `_FakePool`/`_FakeConn` pattern
(`tests/unit/test_summary_rejection_cache.py:163-186`). The vector store is faked at the
`treeweft.retriever` shim. A real LanceDB on `tmp_path` is allowed.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US4 from spec.md

**FR-019** (nothing outside the vector store keeps vector-store row IDs) has no task: research R10
settled it during planning, with evidence. Re-check it only if a task adds a consumer of row IDs.

---

## Phase 1: Setup

**Purpose**: Configuration and schema that everything else reads.

- [X] T001 [P] Add `PROMPT_PINS_REFRESH_SECONDS` (default 5, a positive number) to
  `src/treeweft/infrastructure/config.py`:
  - An invalid value fails `validate_config()` with a message naming the setting
    (constitution V).
  - Add a commented default to `.env.example`.
  - Test in `tests/unit/test_config.py`: the default, an override, and an invalid value
    rejected.
- [X] T002 [P] Write `src/treeweft/adapters/postgresql/migrations/021_prompt_versions.sql` per
  [data-model.md](data-model.md) "Migration 021":
  - `prompt_pins (operation TEXT NOT NULL, scope TEXT NOT NULL, version INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_by TEXT, PRIMARY KEY (operation,
    scope), CHECK (operation <> 'hyde' OR scope = 'deployment'))`;
  - `source_records` gains `summary_prompt_version INTEGER` and `summary_refresh_target INTEGER`;
  - the backfill `UPDATE source_records SET summary_prompt_version = 3 WHERE
    summary_prompt_version IS NULL`.

  Every statement uses `IF NOT EXISTS` (the runner is unlocked; issue #28). No pin rows are
  inserted. The header comment supersedes 005's "bump `PROMPT_VERSION`" note; 005 is not edited.
  Test in `tests/unit/test_migration_021.py`: `021_prompt_versions.sql` exists, the migration
  numbers have no gaps, every
  `CREATE` and `ADD COLUMN` is `IF NOT EXISTS`, and it contains no `INSERT INTO prompt_pins`.

---

## Phase 2: Foundational (blocking prerequisites)

**Purpose**: The registry, the pure resolution rules, pin storage and sync, and the versioned
summary and HyDE paths. Every story needs them.

**⚠️ No user-story work starts until this phase is complete.**

### Registry

- [X] T003 [P] Write `tests/unit/test_prompt_registry.py` (research R1):
  - Every registered version's `sha256(json.dumps({"system", "schema": asdict(...)},
    sort_keys=True, ensure_ascii=False))` equals `tests/unit/fixtures/prompt_hashes.json`.
  - The test fails, naming the version, when:
    - a hash differs (the version was edited);
    - a registered version has no fixture entry;
    - the fixture names an unregistered version.
  - `system_prompt(get("chunk_summary", 3))` and `system_prompt(get("hyde", 1))` equal the
    pre-change `_SUMMARY_SYSTEM` and `_HYDE_SYSTEM`. Copy those as string literals into the test
    **before** T004 deletes them. Check with `LLM_ENABLE_THINKING` both on and off.
  - The schemas equal today's `_SUMMARY_SCHEMA` and `_HYDE_SCHEMA` field by field, and the
    registry copies are frozen (mutation raises).
  - `latest()`, `versions()` and `is_registered()` behave as specified; an unknown operation
    raises `KeyError`.
- [X] T004 Implement `src/treeweft/adapters/llm_api/prompts.py`:
  - `PromptVersion` is a frozen dataclass: `operation`, `version`, bare `system`, a frozen
    `schema`, `notes`.
  - `REGISTRY` holds `chunk_summary` v3 and `hyde` v1, with the texts and schemas copied byte for
    byte from `llm_adapter.py:41-54` and `llm_caller.py:54-71`.
  - Functions: `get`, `versions`, `latest`, `is_registered`, `BASELINE = {"chunk_summary": 3,
    "hyde": 1}`, and `system_prompt(pv)`, which appends `_NO_THINK_SUFFIX`.
  - Write `tests/unit/fixtures/prompt_hashes.json`.

### Pure rules

- [X] T005 [P] Write `tests/unit/test_prompt_pins_domain.py`:
  - `resolve(view, "chunk_summary", source_id)`: an override beats the deployment pin, and the
    deployment pin applies without an override. `resolve(view, "hyde")` returns the deployment
    pin.
  - `is_stale`:
    - it is true when `summary_refresh_target` is set, **even when `summary_prompt_version`
      equals the target** (FR-015);
    - it is true when recorded ≠ effective;
    - it is false when recorded is NULL and the target is NULL.
  - `seed_version(op, histogram, has_sources)`:
    - no sources: latest;
    - existing sources: the most common non-NULL version, with ties going to the higher version;
    - sources but all NULL: latest;
    - `hyde` with sources: 1.
- [X] T006 Implement `src/treeweft/domain/prompt_pins.py`: `PinView` (frozen), `resolve`,
  `is_stale` and `seed_version`, with no adapter imports (constitution IV).

### Postgres storage

- [X] T007 [P] Write `tests/unit/test_prompt_pin_store.py`, with a fake pool:
  - `PromptPinStore.upsert` and `delete` run `NOTIFY prompt_pins_changed` **inside** the same
    transaction as the write (assert the order within one `transaction()` block).
  - `seed_if_absent` issues `INSERT … ON CONFLICT DO NOTHING`.
  - `delete_overrides_for_source` deletes only rows whose scope is that source.
  - `PostgreSourceRepository.mark_summary_refresh(source_id, target)` sets only
    `summary_refresh_target`.
  - `record_summary_version(source_id, v)` sets `summary_prompt_version = v` **and**
    `summary_refresh_target = NULL` in one `UPDATE`.
  - `summary_version_histogram()` groups the non-NULL versions.
  - `save()` still does not write either new column.
- [X] T008 Implement `src/treeweft/adapters/postgresql/prompt_pin_store.py`
  (`NOTIFY_CHANNEL = "prompt_pins_changed"`, `list_all`, `upsert`, `delete`, `seed_if_absent`,
  `delete_overrides_for_source`). Add the three methods to
  `src/treeweft/adapters/sources/repository.py`. Add `summary_prompt_version` and
  `summary_refresh_target` (default None) to `SourceRecord` in
  `src/treeweft/domain/sources/__init__.py`, and read them in `get_by_id` and `list_all`.

### Pin view, seeding and sync

- [X] T009 [P] Write `tests/unit/test_prompt_pins_sync.py`, with a fake pool and a fake listener
  connection:
  - `load_and_seed()`:
    - on an empty table with no sources, seeds latest;
    - with v3 sources, seeds 3 and 1;
    - it logs each seed;
    - two concurrent calls leave one row per operation.
  - A missing `prompt_pins` table or missing columns raise `RuntimeError` mentioning
    "migration 021".
  - A stored deployment pin or override naming an unregistered version raises `RuntimeError`
    naming the pin and the registered versions.
  - A reload after a simulated reconnect picks up a change made while disconnected.
  - The periodic reload picks up a change within `PROMPT_PINS_REFRESH_SECONDS`, using a
    monkeypatched short interval.
  - A reload that changes the HyDE pin clears `llm_adapter._HYDE_CACHE`; one that leaves it
    unchanged does not.
  - With `DATABASE_URL` unset, `effective()` returns `BASELINE`, and `load_and_seed()` writes
    nothing and logs one warning: "prompt pins unavailable without DATABASE_URL; using baseline
    chunk_summary v3, hyde v1" (constitution V: an observable fallback).
- [X] T010 Implement `src/treeweft/application/prompt_pins.py`:
  - the module-level `PinView`, swapped atomically, and `effective(op, source_id=None)`;
  - `load_and_seed()`, per research R9;
  - `start_sync()` and `stop_sync()`, copying `EmbeddingProxy.start_listener`'s dedicated
    connection and backoff (`adapters/tei/embedding_proxy.py:243-320`), and adding a reload on
    every (re)connect plus a periodic reload task;
  - `set_pin` and `clear_override` are added in US2 and US3.

### Versioned LLM paths

- [X] T011 [P] Write `tests/unit/test_summary_versioning.py` (research R2):
  - `cache_put(sha1, s, prompt_version=4)` writes `prompt_version = 4`.
  - `cache_get_many` requires `prompt_version` and filters on it.
  - `summarize_with_cache(..., version=4)` uses v4's prompt and schema (registry monkeypatched
    with a test v4). It returns `(summary, strategy)` with strategy `"cached"`, `"generated"`,
    `"rejected"` or `"error"`. It writes `""` only for `"rejected"`, and writes nothing for
    `"error"`.
  - `generate_hyde` uses `prompt_pins.effective("hyde")`, and its cache key starts with the
    version: the same query under two HyDE pins makes two LLM calls.
  - `generate_summary()` still returns `str | None`.
- [X] T012 Update `src/treeweft/adapters/llm_api/llm_adapter.py` and `llm_caller.py`:
  - delete `PROMPT_VERSION`, `_SUMMARY_SYSTEM`, `_HYDE_SYSTEM`, `_SUMMARY_SCHEMA` and
    `_HYDE_SCHEMA`;
  - route through `prompts` and `prompt_pins`, keeping `_build_summary_user_message` and the
    nonce fence shared (FR-004);
  - update the comments at `:292`, `:298` and `:327`;
  - update the existing tests that import the constants: `test_summary_prompt_injection.py`,
    `test_summary_rejection_cache.py` (including the `_FakeCache` signatures),
    `test_llm_domain.py`, `test_llm_adapter.py` and `test_llm_timeout_queue_wait.py`.

  Run the full unit suite; it must be green apart from the new failing tests of later phases.

**Checkpoint**: registry, resolution, storage and sync work; summaries and HyDE are versioned.

---

## Phase 3: User Story 1 — An upgrade that ships a new prompt changes nothing until the admin opts in (P1) 🎯 MVP

**Goal**:
- Existing deployments are seeded to v3 and v1, and fresh installs to latest.
- Indexing and search use the resolved version.
- Only a clean full index job records a source's version.
- An unknown pin aborts startup.

**Independent Test**: Spec US1: with a test v4 registered, (a) existing v3 sources and no pins
lead to seeds of 3 and 1, nothing stale, and summaries written and read at v3; (b) an empty
deployment is seeded to v4.

### Tests for User Story 1 (write first, confirm they fail)

- [X] T013 [P] [US1] Write `tests/unit/test_full_index_summary_version.py` (research R6,
  FR-010), with a fake vector store and a fake source repo:
  - A clean `repo`, `directory` or `file` job calls `record_summary_version(source, <payload
    version>)`, which also clears the target.
  - A job with a file error does not call it. A job with **one transient summary error**
    (strategy `"error"`) does not call it either, and `job["summary_errors"] == 1`.
  - With `USE_SUMMARY_VECTOR` off, or `summary_vectors_supported()` False, it records `None`.
  - `treeweft.retriever.summary_vectors_supported` exists for milvus, lancedb and chromadb
    (added to `test_store_shim_exports.py`'s `names` here; T021 adds the other four).
  - A `graph` job and an `incremental` job never call either method.
  - The target version is resolved once at job start into `job["payload"]["summary_version"]`.
    A resumed job (`skip_count > 0`) reuses it even after the pin moves, and records that version.
- [X] T014 [P] [US1] Write `tests/unit/test_summary_tail_version.py` (research R8):
  - With no per-request version, tail chunks are read at each source's recorded version, and a
    miss falls back to the effective version. There is one `cache_get_many` call per distinct
    version.
  - An explicit `summary_prompt_version=9002` is passed through unvalidated.
- [X] T015 [P] [US1] Write `tests/unit/test_lifecycle_prompt_pins.py`:
  - `startup()` calls `prompt_pins.load_and_seed()` after `run_migrations()` and before
    `index_guard.run_check()` and the job queue's `start()` (assert the call order).
  - A `RuntimeError` from it propagates, so startup aborts, and it is not logged-and-swallowed.
  - `start_sync()` runs after seeding, and shutdown calls `stop_sync()`.

### Implementation for User Story 1

- [X] T016 [US1] In `src/treeweft/application/indexer_runners.py`:
  - `_summaries_for_chunks(chunks, *, version) -> list[SummaryOutcome]`;
  - the full-job runners (file, directory, repo) resolve `prompt_pins.effective("chunk_summary",
    source_id)` at start into `job["payload"]["summary_version"]`, unless it is already present
    (resume), and persist it;
  - `_process_file` passes that version and counts `"error"` outcomes into
    `job["summary_errors"]`, mirrored into the payload;
  - `_finalize_job` applies research R6's rule for kinds `{"file", "directory", "repo"}` only.

  This task also adds `summary_vectors_supported()` (Milvus and LanceDB return True, ChromaDB
  False) to the three vector-store modules and the `treeweft.retriever` import blocks, because the
  rule needs it. T025–T027 add the other four store functions.
- [X] T017 [US1] In `src/treeweft/application/retrieval.py` (around `:1011`), implement research
  R8's default read version, grouping `cache_get_many` calls by version.
- [X] T018 [US1] In `src/treeweft/application/lifecycle.py`:
  - call `await prompt_pins.load_and_seed()` right after `_seed_admin_if_first_start()`, with no
    try/except (constitution V);
  - then call `await prompt_pins.start_sync()`, and `stop_sync()` in shutdown next to the
    embedding listener (`:594`).
- [X] T019 [US1] Regression proof: revert T016's incremental/graph exclusion locally (let
  `_finalize_incremental_job` call `record_summary_version`), confirm that
  `test_full_index_summary_version.py` fails, then restore. Record this in the PR (constitution
  II, FR-010).

**Checkpoint**: an upgrade is inert; US1's acceptance scenarios 1–7 pass in unit tests.

---

## Phase 4: User Story 2 — An admin moves the chunk-summary pin and only the summaries are refreshed (P1)

**Goal**:
- The vector-store rewrite functions (all three backends).
- The `resummarize` job, with deferral and restart recovery.
- The read endpoint.
- The deployment pin endpoint, with a dry run.

**Independent Test**: Spec US2: with a fake vector store, a v3 source, and a pin moved to v4 by
dry run and then for real:
- the dry run matches the real call and writes nothing;
- the refresh rewrites every summary vector with the row count and code vectors unchanged, and
  no parsing or code embedding;
- the source ends current at v4.

### Tests for User Story 2 (write first, confirm they fail)

- [ ] T020 [P] [US2] Write `tests/unit/test_lancedb_summary_rewrite.py`, on a real LanceDB table
  in `tmp_path` built with `_schema(dim)`:
  - `snapshot_source_row_ids` returns only that source's IDs, and `fetch_rows` returns full rows;
  - `write_summary_vectors(rows, [v, None, …])` updates `summary_vector` in place, and `None`
    stays NULL;
  - IDs, row count, `vector`, `chunk_text` and FTS search results are unchanged;
  - `count_source_rows` matches.

  First try a partial-column `merge_insert` (`id`, `summary_vector`). If lancedb 0.33.0 rejects
  it, the test pins the full-row fallback (research R7).
- [ ] T021 [P] [US2] Extend `tests/unit/test_store_shim_exports.py` `names` with
  `snapshot_source_row_ids`, `fetch_rows`, `write_summary_vectors`
  and `count_source_rows` for milvus, lancedb and chromadb. Add Milvus unit tests to
  `tests/unit/test_milvus_summary_rewrite.py` with a mocked `MilvusClient`:
  - the snapshot uses `query_iterator` with an `_escape_literal`-escaped filter (a `source_id`
    containing `"` and `\`), `output_fields=["id"]` and `consistency_level="Strong"`;
  - `fetch_rows` uses `query(ids=…, output_fields=["*"], consistency_level="Strong")`;
  - `write_summary_vectors` upserts full rows **without** `sparse_vector`, with the dynamic
    fields kept and `None` becoming `[0.0]*dim`;
  - `count_source_rows` uses `Strong`.

  ChromaDB: `summary_vectors_supported()` is False.
- [ ] T022 [P] [US2] Write `tests/unit/test_resummarize_job.py` (research R10), with a fake
  vector store, fake LLM and embedder, and a fake source repo:
  - The no-op check runs first: disabled or ChromaDB gives done with a reason message, and
    neither column changes.
  - The mark (`mark_summary_refresh`) is set **before** the first `write_summary_vectors`.
  - The snapshot is taken once, before any write, and batches follow `EMBED_BATCH_SIZE`.
  - Summaries use the target version, and a cached chunk makes no LLM call.
  - A rejected summary passes `None` and does not count as an error.
  - A clean run calls `record_summary_version(target)`, and the progress counters reach the
    total.
  - One transient error gives done with `errors == 1` and a `job_file_errors` row, and no
    `record_summary_version`.
  - A re-run after that makes LLM calls only for the failed chunk (SC-003).
  - A count mismatch after the loop fails the job with a message naming both counts.
  - A source deleted mid-job gives done with "source deleted", and no further writes.
  - A pin moved while the job was queued is re-resolved at start, and the payload is updated.
  - No parse, code-embedding or graph function is called (SC-002; assert with spies).
- [ ] T023 [P] [US2] Write `tests/unit/test_refresh_hooks.py` (research R4, R5):
  - After a `repo` or `incremental` job finishes (done, errors or failed) on a stale source with
    no active job and a writable index, one `resummarize` is enqueued.
  - None is enqueued when:
    - the source is current;
    - another job is active;
    - the index is not writable;
    - the finished job was itself a `resummarize` (no loop).
  - Startup recovery re-enqueues an interrupted `resummarize`, even when its source has a done
    job. Other kinds keep the "superseded" behaviour.
  - While `reindex_required`, the worker's `dispatch_allowed` refuses a queued `resummarize`.
  - If `enqueue_if_stale` raises, the finished job keeps its status and the error is logged
    (constitution V).
  - Two processes racing to enqueue for one source (the hook in both, or a hook against a pin
    change): the second insert's unique violation on `jobs_active_source_uniq` is caught and
    reported as deferred or already queued, never a 500. Exactly one job exists afterwards.
- [ ] T024 [P] [US2] Write `tests/unit/test_prompt_routes.py` for `GET /prompt-versions` and
  `PUT /prompt-pins/{operation}` ([contracts/http-api.md](contracts/http-api.md)):
  - Non-admins get 401 or 403 on both.
  - Without Postgres, both return 503.
  - GET returns the registry, the pins and each source's recorded/target/effective/stale/active
    job.
  - An unknown version gives 400 with `valid_versions`, for both dry-run and real calls.
  - An unknown operation gives 404.
  - Setting the pin to its current version writes nothing.
  - **Dry run equals real** over a table with:
    - an overridden source (skipped);
    - an already-current source (excluded);
    - a source with an active job (`deferred`);
    - a source whose refresh toward v4 ended with errors (`summary_refresh_target = 4`,
      `summary_prompt_version = 3`) while the pin moves back to 3: it is listed in `enqueued`
      (spec US2 scenario 6, FR-015);
    - the index `reindex_required` (the pin is stored, and every source is in `not_enqueued`
      with the reason).

    The dry run writes no row, sends no `NOTIFY` and creates no job or group. The real call's
    job IDs match the sources the dry run listed (SC-004).
  - Two or more jobs share one `prompt-refresh` group.
  - `updated_by` is the caller.
  - A real pin change logs one line with the caller, the operation and scope, the old and new
    version, and the enqueued, deferred and not-enqueued source IDs. A dry run logs that it was a
    dry run (FR-026).
  - A `hyde` pin change enqueues nothing and returns `effect`, and the HyDE cache is cleared in
    the calling process.
  - `GET /sources` includes `summary_prompt_version`, `summary_refresh_target` and
    `summary_stale`.

  Add the new mutating routes to `tests/unit/test_csrf_surface.py` and
  `tests/unit/test_dev_admin_proxy_hardening.py`.

### Implementation for User Story 2

- [ ] T025 [P] [US2] Milvus: add `MilvusAdapter.snapshot_source_row_ids`, `fetch_rows`,
  `write_summary_vectors` and `count_source_rows`, plus module wrappers, in `src/treeweft/adapters/milvus/vector_store.py`.
  - Use `_execute_with_reconnect` and `consistency_level="Strong"`.
  - The iterator filter is interpolated through `_escape_literal`, because `QueryIterator`
    drops `filter_params`.
- [ ] T026 [P] [US2] LanceDB: add the same four functions in
  `src/treeweft/adapters/lancedb/vector_store.py` (`asyncio.to_thread`; `merge_insert("id")`, or
  the fallback T020 settled; predicates escaped with `_esc`).
- [ ] T027 [P] [US2] ChromaDB: add the unsupported stubs per
  [contracts/store-ports.md](contracts/store-ports.md) in
  `src/treeweft/adapters/chromadb/vector_store.py`, and export the four new functions for every backend in
  `src/treeweft/retriever.py`.
- [ ] T028 [US2] Implement `src/treeweft/application/prompt_refresh.py`:
  - `plan_refreshes(source_ids, target_for)`, which sorts sources into enqueue, defer and refuse
    using `_find_active_job_for_source` and `index_guard.require_writable()`;
  - `enqueue_refreshes(plan, *, created_by)`, which builds `resummarize` jobs with
    `payload.target_version`, uses one `prompt-refresh` group when there are two or more, and
    enqueues through `_state._job_queue`;
  - `enqueue_if_stale(source_id)`.

  An enqueue that loses a race on `jobs_active_source_uniq` is caught and reported as deferred
  (or already queued), never as a 500. The same applies in `enqueue_if_stale`.

  Extend `application/prompt_pins.py` with `set_pin(op, "deployment", version, *, updated_by,
  dry_run)`. It validates, computes the plan, and (if not a dry run) writes, `NOTIFY`s, swaps its
  own view immediately, and enqueues. It returns the contract's result shape, and logs the
  change per FR-026.
- [ ] T029 [US2] In `src/treeweft/application/indexer_runners.py`:
  - add `kind == "resummarize"` to `dispatch_job`;
  - implement `_run_resummarize_job(job)` per research R10 steps 0–5, using T016's
    `_summaries_for_chunks`, the embedding path `_process_file` uses, and the job counters from
    research R12 (the `total_files`/`processed_files` chunk units, `total_chunks`, and `message`
    `"refreshed N/M chunks to chunk_summary vK"`).
- [ ] T030 [US2] Call `prompt_refresh.enqueue_if_stale(job.source_id)` after every terminal
  status of a non-`resummarize` job in `src/treeweft/adapters/queue/postgres_queue.py`
  (`_run_one`, including the dead-letter path), guarded so that a failure there is logged and
  never fails the finished job. Exempt `kind == "resummarize"` from the "superseded" rule in
  `src/treeweft/application/lifecycle.py` (`:492-500`).
- [ ] T031 [US2] Create `src/treeweft/application/routes_prompts.py` with `GET /prompt-versions`
  and `PUT /prompt-pins/{operation}` (`authz._require_admin`, `_caller_id`, `dry_run: bool =
  False`, 200 for both). Return 400 as `JSONResponse({"detail", "valid_versions"})`, and 503
  without Postgres. Register the router next to `indexer_service.py:160`. Add the three summary
  fields to `GET /sources`.
- [ ] T032 [US2] Regression proofs, each shown failing and then restored, and recorded in the PR:
  - remove the `summary_refresh_target` term from `is_stale`: T005 and T024's pin-back case
    (spec US2 scenario 6) fail (FR-015);
  - let the dry-run branch call `store.upsert`: T024 fails.

**Checkpoint**: adopting a new version through the deployment pin works end to end against
fakes; US2's acceptance scenarios 1–9 pass.

---

## Phase 5: User Story 3 — An admin trials a version on one source before promoting it (P2)

**Goal**:
- Set and clear per-source overrides, with a dry run.
- A HyDE override is refused.
- Manual refresh.
- An override is deleted with its source.

**Independent Test**: Spec US3: with two v3 sources, an override of v4 on one refreshes only
that source. A later deployment move skips it. Clearing the override makes it follow the
deployment pin, with no job when it is already there.

### Tests for User Story 3 (write first, confirm they fail)

- [ ] T033 [P] [US3] Write `tests/unit/test_prompt_override_routes.py`:
  - `PUT /prompt-pins/chunk_summary/sources/{id}`, dry run then real: only that source appears;
    an unknown version gives 400 with `valid_versions`; an unknown source gives 404.
  - `PUT /prompt-pins/hyde/sources/{id}`, dry run or not, gives 400.
  - `DELETE` on an override gives the deployment pin as `version`, and a job only if stale. It
    returns 404 without an override.
  - A deployment-pin change skips an overridden source.
  - `POST /sources/{id}/resummarize` returns:
    - 202 with a job when stale;
    - 200 "already current" when current;
    - 200 deferred with an active job;
    - 409, the guard's body, when the index is not writable;
    - 404 for an unknown source;
    - 202 (not "already current") for a source whose refresh target is set at its recorded
      version.
  - `DELETE /sources/{id}` deletes the source's override row.
  - All of these routes are admin-only.
  - Override changes and manual refreshes log the caller, the source, the old and new version,
    and the outcome (FR-026).

### Implementation for User Story 3

- [ ] T034 [US3] Add `set_pin(op, source_id, …)` validation (a `hyde` override gives 400) and
  `clear_override(source_id, …)` to `src/treeweft/application/prompt_pins.py`. Add the `PUT` and
  `DELETE /prompt-pins/chunk_summary/sources/{source_id}` routes and
  `PUT /prompt-pins/hyde/sources/{source_id}` (always 400) to `routes_prompts.py`.
- [ ] T035 [US3] Add `POST /sources/{source_id}/resummarize` to `routes_prompts.py`. It returns
  the guard's 409 as-is (`await index_guard.require_writable()`), then applies T028's plan for a
  single source, and logs the request per FR-026.
- [ ] T036 [US3] In `remove_source` (`src/treeweft/application/indexer_service.py:1247-1293`),
  call `PromptPinStore.delete_overrides_for_source(source_id)` next to `_source_repo.delete`.
  **Not** in the pre-reindex `graph_store.delete_source` path (`indexer_runners.py:766`).

**Checkpoint**: trialling on one source and promoting works; US3's acceptance scenarios 1–6
pass.

---

## Phase 6: User Story 4 — Operators see and manage prompt versions in the UI (P3)

**Goal**: a Prompts page with an inline dry-run confirmation (research R14).

**Independent Test**: Spec US4: with the API mocked, the page shows versions, notes, pins and
staleness. A change calls the dry run first and shows its result, Cancel sends nothing, and
Confirm sends the real request.

### Tests for User Story 4 (write first, confirm they fail)

- [ ] T037 [P] [US4] Write `ui/src/api/prompts.test.ts`, covering the pure helpers:
  - the stale count;
  - the total-chunks formatting;
  - parsing `valid_versions` from an `ApiError` body.
- [ ] T038 [P] [US4] Write `ui/src/pages/PromptsPage.test.tsx`, modelled on
  `BackendsPage.test.tsx` (`vi.mock("@/api/client")`):
  - It renders each operation's versions, notes, latest version and deployment pin, and each
    source's built/target version, stale badge and override control.
  - Choosing a new deployment pin issues `PUT …?dry_run=true` first and shows the enqueued,
    deferred and not-enqueued sources with their chunk counts and total.
  - Cancel sends no further request.
  - Confirm sends the real `PUT` and shows the job IDs.
  - The override set and clear flows work the same way.
  - The refresh action calls `POST /sources/{id}/resummarize`.
  - A 401 or 403 renders "Admin access required".
  - A 400 shows the valid versions.

### Implementation for User Story 4

- [ ] T039 [US4] Implement `ui/src/api/prompts.ts` (types from
  [contracts/http-api.md](contracts/http-api.md), fetchers, helpers) and
  `ui/src/pages/PromptsPage.tsx`, with an inline confirmation panel. Register `{ path:
  "prompts" }` in `ui/src/routes/router.tsx`, and add the Prompts tab after Backends in
  `ui/src/components/layout/AppShell.tsx`.
- [ ] T040 [P] [US4] In `ui/src/pages/JobDetailPage.tsx` and `JobsPage.tsx`, label progress units
  "chunks" when `kind === "resummarize"` (research R12), with a test in each page's existing test
  file.

**Checkpoint**: `cd ui && npm test && npm run build` passes.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T041 [P] Write `tests/integration/test_resummarize_milvus.py` (`@pytest.mark.slow`, skipped
  unless `MILVUS_TEST_URI`):
  - Use a per-run collection `itest_resum_<hex>` through `MilvusAdapter(collection_name=…)`,
    never the module wrappers. Drop it before and after.
  - Insert rows with the production schema, including a dynamic field, then run the adapter
    snapshot → fetch → write → count.
  - Assert:
    - the row count is unchanged, with no duplicate `(file_path, start_line)`;
    - the new `summary_vector` values are present;
    - `vector`, `chunk_text` and the dynamic field are unchanged;
    - dense and BM25 search still return the chunks;
    - the snapshot iterator honours `Strong`.
- [ ] T042 [P] Write `tests/integration/test_prompt_pins_pg.py` (`slow`, skipped unless
  `POSTGRES_TEST_URL`). Use only uniquely prefixed scopes (`itest-<hex>-…`) and delete them in
  teardown. Assert:
  - `NOTIFY` reaches a second connection's listener;
  - the `CHECK` rejects a HyDE override;
  - a concurrent `seed_if_absent` from two connections leaves one row.

  Never touch the `deployment` rows.
- [ ] T043 [P] Update `docs/engineering-notes.md:325`: replace "Bump `PROMPT_VERSION`…" with the
  registry rule and the rollout flow, and describe the pins, the refresh job and staleness.
  Write the new runbook `docs/prompt-versions.md`:
  - trial with an override, compare, promote, and watch the `prompt-refresh` group;
  - deferred and not-enqueued refreshes;
  - retrying a refresh that ended with errors;
  - the no-Postgres baseline;
  - fail-loud startup after a downgrade.

  Cross-link it from `docs/engineering-notes.md`.
- [ ] T044 [P] Add a `CLAUDE.md` Invariant: "Never edit a registered prompt version in
  `adapters/llm_api/prompts.py`. Add a new version and its `tests/unit/fixtures/prompt_hashes.json`
  entry. `test_prompt_registry.py` guards this. Making a new version a fresh install's default
  needs a benchmark run (constitution III)."
- [ ] T045 [P] Update ADR-003's status to "Accepted and implemented (1.1.0)" in
  `docs/adr-003-prompt-versioning.md`, and add `## Unreleased` entries to `CHANGELOG.md`:
  - **Added**: the prompt registry and pins, `GET /prompt-versions`, the `/prompt-pins/*`
    endpoints with `dry_run`, `POST /sources/{id}/resummarize`, the `resummarize` job, the
    Prompts page, and the `/sources` summary fields.
  - **Operator configuration**: migration 021, pins seeded on first start, nothing changing until
    an admin moves a pin, `PROMPT_PINS_REFRESH_SECONDS`, and startup failing on an unknown pinned
    version.

  Confirm that `pyproject.toml` is still 1.1.0 and that `v1.1.0` is not tagged (`git tag -l
  v1.1.0`). If it is tagged, bump to 1.2.0 instead (research R13). Do **not** run
  `scripts/update_contracts.py`.
- [ ] T046 Run the full unit suite, `env -u PYTHONPATH python -m pytest tests/unit -q`, and
  `cd ui && npm test && npm run build`. Record the pass and fail counts, including
  `test_contracts.py`'s version check.
- [ ] T047 Run quickstart §3's integration tests against the homelab Milvus and Postgres
  (10.16.1.226). If a host is unreachable, say so and ask; do not work around it. Record the
  results, or the reason for a skip.
- [ ] T048 Run quickstart §4's live end-to-end check, after verifying the served LLM, embedding
  model and reranker. Record each step's HTTP codes and observed values for the PR, including
  SC-001's identical top-10 for 3 queries, SC-002's counters, SC-003's approximately zero LLM
  calls, and SC-008's two-process convergence.

---

## Dependencies & Execution Order

### Phase dependencies

- Setup (T001–T002) → Foundational (T003–T012) → US1 (T013–T019) → US2 (T020–T032) → US3
  (T033–T036). US4 (T037–T040) needs only the US2 and US3 API contracts, so it can be built in
  parallel against mocks once T024 and T033 fix the shapes. Polish comes last.
- US2 needs US1's `_summaries_for_chunks(version)` (T016), its full-job bookkeeping, and the
  lifecycle wiring (T018).
- US3 extends US2's `prompt_pins.set_pin`, `prompt_refresh` and `routes_prompts.py`.

### Within phases

- Foundational:
  - T004 comes before T012.
  - T006 comes before T008 and T010.
  - T010 comes before T012, because `generate_hyde` reads `effective()`.
  - The test tasks T003, T005, T007, T009 and T011 can all be written in parallel first.
- US1: T016, T017 and T018 edit different files. T019 follows T016.
- US2:
  - T025, T026 and T027 (the backends) run in parallel.
  - T028 comes before T029, T030 and T031.
  - T029 edits `indexer_runners.py` after T016 (the same file, so they are sequential).
  - T030 edits `postgres_queue.py` and `lifecycle.py`, after T018.
- US3: T034 and T035 both edit `routes_prompts.py`, so they are sequential. T036 is a separate
  file.
- US4: T039 comes before T040's test run. T037 and T038 are written first.

### Parallel opportunities

```text
Setup:         T001 | T002
Foundational:  T003 | T005 | T007 | T009 | T011   (tests), then T004 | T006, then T008 | T010, then T012
US1 tests:     T013 | T014 | T015
US2 tests:     T020 | T021 | T022 | T023 | T024
US2 impl:      T025 | T026 | T027, then T028 → T029 → T030 → T031
US4:           T037 | T038, alongside US3 once the contracts are fixed
Polish:        T041 | T042 | T043 | T044 | T045
```

## Implementation Strategy

- **MVP**: Setup, Foundational and US1. Prompts become versioned and pinned, an upgrade is inert,
  and version bookkeeping is correct. This alone removes "a prompt change forces a re-index" as a
  release blocker, because a new version can ship without being adopted.
- **Increments**: US1 → US2 (adopting a version, which is the cost saving) → US3 (a safe trial on
  one source) → US4 (UI) → Polish. A single PR is expected, as for 001. Commit after each task or
  test/implementation pair.
- **Verification**: T019 and T032 (the regression proofs) and T046–T048 (quickstart), all
  reported in the PR under constitution I.
