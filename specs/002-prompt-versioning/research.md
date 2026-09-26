# Research: Prompt Versioning with Admin Pins and Summary-Only Refresh

Phase 0 of [plan.md](plan.md). Each item records a decision, the reasons for it, and the
alternatives considered. File references are to `main` at `d43b1a5`. Items marked *unverified*
are checked by a named test before anything relies on them.

Five findings contradict or extend ADR-003 as written. ADR-003 is amended in this PR to match
(constitution, "Development Workflow"):

- R4: jobs are not serialized;
- R5: interrupted refreshes are dropped at startup;
- R6: a "clean" full index can hide summary failures;
- R7: ChromaDB has no summary vector;
- R3: missed notifications are lost.

## R1. Registry shape and the immutability hash

**Decision**

- New module `src/treeweft/adapters/llm_api/prompts.py`:
  - `PromptVersion` is a frozen dataclass: `operation`, `version`, `system` (the **bare** text),
    `schema` (a frozen copy of `ResponseSchema`), and `notes`.
  - `REGISTRY: Mapping[str, Mapping[int, PromptVersion]]` is seeded with `chunk_summary` v3 and
    `hyde` v1, the texts and schemas copied byte for byte from `llm_adapter.py:41-54` and
    `llm_caller.py:54-71`.
  - Helpers: `get(operation, version)`, `versions(operation)`, `latest(operation)`, and
    `is_registered(operation, version)`.
- `_NO_THINK_SUFFIX` is appended **at call time** (`system_prompt(pv) = pv.system +
  _NO_THINK_SUFFIX`). The HyDE language suffix is appended after that, exactly as today
  (`llm_adapter.py:154-156`).
- Hash: `sha256(json.dumps({"system": pv.system, "schema": asdict(pv.schema)}, sort_keys=True,
  ensure_ascii=False))`. It is compared against `tests/unit/fixtures/prompt_hashes.json`
  (`{"chunk_summary": {"3": "<hex>"}, "hyde": {"1": "<hex>"}}`).
  - The test fails when a registered version has a different hash (the version was edited).
  - It fails when a registered version has no hash (added without its fixture entry).
  - It fails when the fixture names a version that is not registered (removed).
- A second test asserts that today's rendered prompts are unchanged: `system_prompt(get("chunk_summary", 3))`
  equals the pre-change `_SUMMARY_SYSTEM`, captured as a literal in the test. This proves
  FR-002 (existing cache rows stay valid).

**Rationale**

- `_SUMMARY_SYSTEM` and `_HYDE_SYSTEM` already contain the suffix, and the suffix depends on the
  environment (`LLM_ENABLE_THINKING`). Hashing the concatenated text would make the fixture
  depend on the environment.
- `ResponseSchema` is a mutable, non-frozen dataclass (`domain/response_validator.py:23`), but
  every field is JSON-native, so `asdict` plus `sort_keys` is deterministic. Freezing the
  registry copy (tuples for list fields) stops a runtime mutation from silently changing a
  shipped version. List order is part of the version, so it is kept.

**Alternatives considered**

- Hashing source-code text: this breaks on formatting-only edits and misses schema changes.
- Storing prompts in Postgres: rejected by the ADR ("Admin-authored prompts").

## R2. Where the resolved version is threaded

**Decision**: the version becomes an explicit parameter everywhere, never a hidden global:

| Function | Change |
|---|---|
| `llm_adapter._generate_summary(chunk_text, language, file_path, *, version)` | Prompt and schema come from `prompts.get("chunk_summary", version)` |
| `llm_adapter.summarize_with_cache(..., *, version) -> SummaryOutcome` | Returns `(summary \| None, strategy)`, where strategy is `"cached" \| "generated" \| "rejected" \| "error"`. `generate_summary()` keeps its `str \| None` return for other callers |
| `llm_adapter.cache_put(sha1, summary, *, prompt_version)` | Required keyword. The constant is gone |
| `llm_adapter.cache_get_many(sha1s, prompt_version, include_rejected=False)` | `prompt_version` becomes required. There are three callers, all updated |
| `llm_adapter.generate_hyde(query, language)` | Reads `pins.effective("hyde")`. Cache key `f"{version}\|{language or ''}\|{query}"` |
| `indexer_runners._summaries_for_chunks(chunks, *, version) -> list[SummaryOutcome]` | Called with the job's target version (R6) |
| `retrieval.py:1011` summary tail | See R8 |

`PROMPT_VERSION`, `_SUMMARY_SYSTEM`, `_HYDE_SYSTEM`, `_SUMMARY_SCHEMA` and `_HYDE_SCHEMA` are
deleted. Tests that import them (`test_summary_prompt_injection.py`,
`test_summary_rejection_cache.py`) move to registry lookups. `_build_summary_user_message` and
the nonce fence are unchanged and shared (FR-004).

**Rationale**: an explicit version makes FR-007 testable ("the cache write uses the resolved
version") and stops a code path from silently falling back to a default. The strategy output
exists because R6 and FR-014 must tell "rejected" (deterministic, cached as `""`) from "error"
(transient, not cached), and `summarize_with_cache` hides that today (`llm_adapter.py:331`).

## R3. Pin propagation across processes

**Decision**: a new `application/prompt_pins.py` holds an immutable `PinView` (the deployment
pins and the override map) behind a module reference that is swapped atomically. Resolution
(`effective(op, source_id=None)`) is a dict lookup. The view is refreshed three ways:

1. At startup, before the job queue starts (R9).
2. `LISTEN prompt_pins_changed` on a dedicated asyncpg connection. This copies
   `EmbeddingProxy.start_listener` (`adapters/tei/embedding_proxy.py:243-320`) and adds **a
   reload after every (re)connect**.
3. A periodic reload every `PROMPT_PINS_REFRESH_SECONDS` (default 5; one query over a table of a
   few rows).

When a reload changes the HyDE pin, `_HYDE_CACHE` is cleared. Correctness does not depend on the
clear, because the cache key includes the version (R2); the clear only frees memory.

**Rationale**: the embedding-backends listener never reloads after a reconnect, so a
notification sent while it was disconnected is lost until the next one. SC-008 needs 5 s
convergence. With polling as the floor and the listener for promptness, a lost notification costs
at most 5 s. ADR-003 §1 is amended to name the fallback.

**Alternatives considered**

- Polling only: simpler, but a pin change takes up to 5 s to take effect even in the process that
  made it. The writing process therefore also swaps its own view immediately after committing.
- Listener only: it is lossy, as shown above.

## R4. Jobs are not serialized: deferred refreshes

**Finding**: ADR-003 §3 says migration 008 "serializes" a refresh behind other jobs on the source.
It does not:

- `jobs_active_source_uniq` rejects a second active job for a source;
- the enqueue paths deduplicate and return the existing job
  (`routes_webhook._find_active_job_for_source`, `indexer_service.py:797`).

**Decision**

- A pin mutation or manual refresh that finds an active job on a stale source does not enqueue.
  Its response lists the source under `deferred` with the blocking job's ID.
- When a job of any kind other than `resummarize` finishes (done, done with errors, or failed),
  the worker calls `prompt_refresh.enqueue_if_stale(source_id)`. That enqueues a refresh if the
  source is stale, has no active job, and the index is writable.
- After a `resummarize` job the hook runs only when the source's effective version (re-read from
  Postgres) differs from that job's target, i.e. the refresh was overtaken by a pin change. A
  refresh that keeps failing toward the current pin therefore cannot requeue itself in a loop
  (amended after code review). A retry after errors is manual, or follows the next pin change.

**Preemption (code-review decision, 2026-09-26)**: the reverse direction matters too. Index work
that finds an active `resummarize` must not be deduplicated against it, because a refresh does not
cover code changes. `prompt_refresh.preempt_active_refresh` cancels a queued refresh
(`UPDATE … WHERE status='queued'`) and enqueues the index job; for a running refresh it persists
the index job with status `waiting` and `payload.after_job` set to the refresh (outside the
one-active-job index, and not in `job_queue`). The refresh checks for a waiting job at each batch
boundary and stops, leaving its mark set. After any job finishes, the worker promotes the oldest
job waiting on it and relinks the rest behind that one, so pushes run in order and none is lost;
then the refresh hook re-plans. Startup recovery promotes a waiting job whose predecessor is no
longer active. Migration 022 indexes `payload->>'after_job'` for waiting jobs.

**Rationale**: it keeps the FR-016 promise that every stale source gets a refresh. Queued jobs
cannot be reordered, and the hook needs no new table. A full index job on a stale source
normally makes the source current itself (R6), so the hook is then a no-op.

**Alternatives considered**

- Report only, with no hook: stale sources would sit until an admin noticed.
- Queueing behind the active job: this needs a second queue or dropping the 008 unique index. The
  one-active-job rule protects against concurrent writers on a source, so it stays.

## R5. Interrupted refreshes at startup

**Finding**: startup recovery marks every queued or running job whose source has any `done` job as
"Superseded by prior completed index for this source" (`lifecycle.py:492-500`). An interrupted
`resummarize` would therefore be dropped silently. The FR-015 mark keeps the source stale, but
nothing would re-run the job.

**Decision**: startup recovery exempts `kind == "resummarize"`. Such jobs are re-enqueued like
any job with no prior completion. A re-run redoes the source cheaply (summary cache hits).

## R6. When a full index job may advance the recorded version

**Findings**

- Every full job (file, directory, repo) finishes through `_finalize_job`
  (`indexer_runners.py:831`), which graph-only jobs also use.
- A transient summary failure becomes `None` in `_summaries_for_chunks` and a zero vector in the
  store, and is **not** counted in `job["errors"]`. So `errors == 0` does not mean every chunk was
  summarized.
- `/index-file` derives the source ID from the file path (`indexer_service.py:796`), so a file job
  covers its whole source.

**Decision**

- A full job resolves its target version once, at job start: the source's effective version. It
  stores the version in `job["payload"]["summary_version"]`, so a resumed job keeps using it.
- `_process_file` counts transient summary failures in `job["summary_errors"]` (in memory and in
  the payload; no new column).
- `_finalize_job`, for `kind in {"file", "directory", "repo"}` only, applies these rules:
  - `USE_SUMMARY_VECTOR` on, the store supports summary vectors, `errors == 0` and
    `summary_errors == 0`: set `summary_prompt_version = <job's version>` and clear
    `summary_refresh_target`, in one statement.
  - Summary vectors off or unsupported (R7): set `summary_prompt_version = NULL` and clear the
    mark. The source now holds no summary vectors, which is the ADR's meaning of NULL ("never
    summarized").
  - Anything else: change neither column.
- **Found during implementation (T016):** only `repo` jobs summarize (through `_walk_and_index`
  and `_process_file`). `file` and `directory` jobs insert chunks with no summary vectors. So
  those two kinds follow the "summary vectors off" branch and record `NULL`. Recording their
  version would report empty summary vectors as current.
- The `graph` and `incremental` kinds never touch either column (FR-010). Incremental jobs
  already bypass the source registry (`_finalize_incremental_job`, `indexer_runners.py:796`).
- If the effective version changed during the job, the job still records the version it used, so
  the source correctly shows as stale afterwards.

ADR-003 §2 is amended with the clean-full-job rule and the NULL case.

## R7. Summary-vector support differs by vector store

| Backend | Summary vector | "No summary" stored as | Rewrite mechanism | Row IDs |
|---|---|---|---|---|
| Milvus 2.5.4 | yes, non-nullable | zero vector (`vector_store.py:343-347`) | full-row `upsert` without `sparse_vector` (partial upsert arrives in 2.6) | re-keyed (auto-ID) |
| LanceDB 0.33.0 | yes, nullable | NULL (`lancedb/vector_store.py:233-239`) | `merge_insert("id").when_matched_update_all()` | unchanged (client uuid4) |
| ChromaDB 1.5.9 | **none**: `insert_chunks` drops `summary_embeddings` (`chromadb/vector_store.py:241-258`) | n/a | none | n/a |

**Decision**

- New shim function `summary_vectors_supported() -> bool`: Milvus and LanceDB return `True`;
  ChromaDB returns `False`.
- A refresh when `not (USE_SUMMARY_VECTOR and summary_vectors_supported())` is a reported no-op
  (FR-018): done, message "summary vectors disabled" or "not supported by chromadb", and neither
  column changed.
- The per-store "no summary" representation stays inside the store function (`None` in means
  zero on Milvus, NULL on LanceDB). The job never builds zero vectors itself.

ADR-003 §3 is amended to add ChromaDB. The ADR's "same zero vector" wording is generalized to
"the store's own no-summary value".

*Unverified*: whether LanceDB's `merge_insert` accepts a partial column set (`id`,
`summary_vector`). The LanceDB unit test on a real temp table decides it. The fallback is to
merge full rows fetched by ID.

## R8. The summary tail's default read version

**Finding**: `retrieval.py:1011` calls `cache_get_many(tail_shas, prompt_version=summary_prompt_version)`.
With `None`, this reads the constant 3 today. The benchmark reads tier 9002, which is unregistered
(`agentic_runner.py:264-266`).

**Decision**

- An explicit per-request `summary_prompt_version` is passed through unchanged, and is **not**
  validated against the registry (FR-007, "keeps working unchanged").
- With none given, each tail chunk is read at its source's **recorded** version, falling back to
  the source's effective version on a miss. Chunks are grouped by version, so there is one
  `cache_get_many` call per distinct version, normally one.

**Rationale**: the recorded version is the version that built the chunk's stored summary vector,
so the text shown matches what ranked it. The fallback covers chunks refreshed mid-migration.
Validation applies only to pins (FR-024); read overrides are a benchmark and debugging tool.

## R9. Seeding, fail-loud validation and missing Postgres

**Decision**: a new startup step `prompt_pins.load_and_seed()` runs in `lifecycle.startup()`
after `run_migrations()` and the admin seed, and before `index_guard.run_check()` and the queue.
It fails loud in every case below.

1. If the `prompt_pins` table or the new `source_records` columns are missing, it **raises**
   `RuntimeError` naming migration 021. This guards issue #28, where a failed migration is
   swallowed.
2. For each operation without a `deployment` row, it computes the seed from the ADR-003 §4 rules
   plus the edge case in spec FR-013. For `chunk_summary`, that is the most common non-NULL
   `summary_prompt_version` (ties go to the higher version), or `latest` when there are no sources
   or none recorded. It then runs `INSERT … ON CONFLICT DO NOTHING` with `updated_by = NULL`,
   re-reads the table, and logs `seeded prompt pin <op>=<v> (<reason>)`. Two processes starting
   together converge on one row.
3. If any stored pin, or override, names an unregistered version, it **raises** `RuntimeError`
   naming the pin and the registered versions.
4. It starts the listener and the refresh loop (R3).

**No Postgres** (`DATABASE_URL` unset): there are no source records, summary cache or pins.
- Resolution uses the **baseline** versions, `chunk_summary` v3 and `hyde` v1: the versions a
  deployment used before this feature. So an upgrade without Postgres changes no prompt.
- The pin endpoints return 503 "prompt pins need Postgres", like `/embedding-backends`
  (`indexer_service.py:417`).
- Simple mode runs Postgres (`docs/simple-mode.md:17`), so it gets full pin support.

## R10. The refresh job

**Decision**: new kind `resummarize`, dispatched in `indexer_runners.dispatch_job`. The target
version travels in `job.payload.target_version`, resolved at enqueue. The runner is
`_run_resummarize_job(job, source_id, target)`:

0. **No-op check** (R7), then set `summary_refresh_target = target` for the source (FR-015),
   before any write.
1. **Snapshot**: `ids = await retriever.snapshot_source_row_ids(source_id)`. On Milvus this is
   `query_iterator(filter=source_id == "<escaped>", output_fields=["id"],
   consistency_level="Strong")`. The filter is interpolated through `_escape_literal`, because the
   iterator does not forward `filter_params` (`vector_store.py:~500`). Total is `len(ids)`.
2. For each batch of `EMBED_BATCH_SIZE` IDs:
   - `rows = await retriever.fetch_rows(ids)`. On Milvus this is `query(ids=…,
     output_fields=["*"], consistency_level="Strong")`, which also carries any dynamic fields.
   - `outcomes = await _summaries_for_chunks(rows_as_chunks, version=target)`, using the same
     path as indexing, with cache hits and rejection markers reused.
   - The non-empty summaries are embedded through the embedding proxy, as `_process_file` does
     today.
   - `await retriever.write_summary_vectors(rows, vectors)`, with `None` where there is no
     summary. On Milvus this is `upsert` of the full row without `sparse_vector`; on LanceDB it
     is `merge_insert` by `id`.
   - Progress: `processed_files += len(batch)` (R12). Any `"error"` outcome adds to `errors` and
     records a `job_file_errors` row for the chunk's file (`error_kind="exception"`).
   - **Amended after code review:** a chunk whose summary failed transiently keeps its existing
     `summary_vector` (written back from the fetched row); only a rejection gets the store's
     no-summary value. The source is re-checked immediately before and after each write; if it
     was deleted, nothing more is written and the source's rows are deleted again, because a
     Milvus upsert re-inserts missing primary keys. The job re-reads the pins before resolving
     its target, and ends `done` without writing when the source is already cleanly at it.
3. **Verify**: `count_source_rows(source_id)` at `Strong` must equal `len(ids)`. A mismatch fails
   the job loudly, naming the counts (constitution V). This catches duplicates or losses from
   re-keying.
4. **Finish**: if `errors == 0`, set `summary_prompt_version = target` and clear the mark in one
   statement. Otherwise the job ends `done` with errors, or `failed` when every chunk failed, and
   both columns stay as they are.
5. A source deleted mid-job (`source_records` row gone) ends the job `done` with the message
   "source deleted", and nothing more is written.

**Before the loop**: if the source's effective version no longer equals `target` when the job
starts (the pin moved while it was queued), it re-resolves and uses the current effective
version. The payload is updated.

**Store functions** (shim pattern from 001; `test_store_shim_exports.py` extended):

| Function | Milvus | LanceDB | ChromaDB |
|---|---|---|---|
| `summary_vectors_supported()` | True | True | False |
| `snapshot_source_row_ids(source_id)` | query_iterator, Strong | `where(source_id = '<esc>')`, select `id` | `[]` |
| `fetch_rows(ids)` | `query(ids, ["*"], Strong)` | `where(id IN …)` | `[]` |
| `write_summary_vectors(rows, vectors)` | full-row `upsert` (no `sparse_vector`) | `merge_insert("id")` | raises `NotImplementedError` (never called) |
| `count_source_rows(source_id)` | `query(filter, output_fields=["count(*)"], Strong)` | `count_rows(predicate)` | n/a |

The Milvus logic lives on `MilvusAdapter` with thin module wrappers, so the integration test can
target a per-run collection (the pattern in `tests/integration/test_index_stamp_milvus.py`).

**FR-019 is resolved**: nothing outside the vector store persists vector-store row IDs.
- The search audit (migration 015) stores none.
- The facet `hit_id` is `file_path:start-end` (`domain/shared.py:96`).
- `hydrate_chunks` and `get_chunk_bodies` key on path and line range.
- The benchmark's IDs are graph entity IDs.
- The HyDE cache and the summary cache are keyed on content.
- The only row-ID uses are within a single request (LanceDB RRF fusion, the Chroma normalizer).

## R11. Admin API placement and errors

**Decision**

- A new router `application/routes_prompts.py`, registered next to the auth and webhook routers
  (`indexer_service.py:160`).
- Every handler starts with `authz._require_admin(request)`. `updated_by` comes from
  `_caller_id(request)`.
- The read endpoint lives at `GET /prompt-versions`, deliberately **not** under `/sources`: GET
  requests under `/sources` skip the auth middleware (`indexer_service.py:157`), which would make
  `_require_admin` return 401 for valid admins.
- The manual refresh is `POST /sources/{source_id}/resummarize`. It is a POST, so it is
  authenticated.
- Errors:
  - 400 is a `JSONResponse` of `{"detail", "valid_versions"}` for an unknown version, and
    `{"detail"}` for a HyDE override;
  - 404 for an unknown source or operation;
  - 503 without Postgres.
- The dry run is a `dry_run: bool = False` query parameter, as on `/index/rebuild`. Both dry and
  real calls return 200: a pin write is synchronous, and the jobs are listed.
- The index guard: pin mutations still store the pin while the index is not writable. They call
  `index_guard.require_writable()` only to decide whether to enqueue, and report the refused
  sources under `not_enqueued` with the guard's reason (FR-017). The manual refresh returns the
  guard's 409 as is.
- The new paths match none of the route-gate prefixes (`test_index_gate_routes.py:172-197`), so
  the gating tests are explicit. The CSRF and admin-surface lists
  (`test_csrf_surface.py:35`, `test_dev_admin_proxy_hardening.py:6`) gain the new mutating routes.

## R12. Refresh progress in the existing job fields

**Decision**: a `resummarize` job uses `total_files` and `processed_files` as unit counters for
**chunks**. `total_chunks` equals the snapshot size, and `message` reads
`"refreshed N/M chunks to chunk_summary vK"`. The UI (`JobDetailPage.tsx:114`, `JobsPage.tsx:136`)
labels the unit "chunks" when `kind === "resummarize"`, a one-line conditional.

**Alternatives considered**: a new progress column, which needs a migration and API change for
cosmetics only.

## R13. Versioning and the changelog

**Finding**: 1.1.0 is not released. No `v1.1.0` tag exists, `contracts/*.json` still record 1.0.0,
and `CHANGELOG.md` holds 001 under `## Unreleased`. The contract check compares against 1.0.0, so
new additive endpoints at 1.1.0 pass.

**Decision**

- Keep `pyproject.toml` at 1.1.0 and add to `## Unreleased`: the pins API, the Prompts page and
  the `resummarize` job under Added; migration 021, the startup seed, and "nothing changes until
  an admin moves a pin" under Operator configuration.
- If 1.1.0 is tagged before this merges, bump to 1.2.0 in this PR.
- Do not run `scripts/update_contracts.py`, which is release-PR only.
- `INDEX_SCHEMA_VERSION` is unaffected: `prompt_pins` and the new `source_records` columns are
  Postgres, outside `index_schema_surface()` (`contracts.py:342`). Spec FR-031 and its assumption
  are corrected to say this.

## R14. UI

**Decision**

- `ui/src/pages/PromptsPage.tsx` plus a test, and `ui/src/api/prompts.ts` plus a test (pure
  helpers), modelled on `BackendsPage` and `api/embeddingBackends.ts`.
- The route is `/prompts` (`routes/router.tsx`), and a tab goes after Backends
  (`components/layout/AppShell.tsx:12`).
- Admin gating matches BackendsPage: the tab is visible, and a 401 or 403 renders
  `<AdminRequired />`. Hiding tabs by role would be a new AppShell pattern; spec US4 scenario 4
  is reworded to match.
- The dry-run confirmation is an **inline panel** on the page, not a modal. It shows the sources
  affected, each one's current and target version and chunk count, and the total, with Confirm
  and Cancel. There is no generic dialog component (`components/ui/` has none), and an inline
  panel is simpler to test.
- The tests mock `@/api/client` as `BackendsPage.test.tsx` does. They assert that a dry-run PUT
  precedes any real PUT, and that Cancel sends nothing.

## R15. Docs

- `docs/engineering-notes.md:325`: replace the "bump `PROMPT_VERSION`" sentence with the registry
  rule and the rollout flow. Lines 321-322 stay accurate.
- New runbook `docs/prompt-versions.md`: trial on one source, compare, promote, and watch jobs.
  It also covers deferred and not-enqueued refreshes and the no-Postgres baseline.
- A new `CLAUDE.md` invariant: never edit a registered prompt version; add a new one with its hash
  fixture entry. `CLAUDE.md`, `CONTRIBUTING.md` and `README.md` never mention `PROMPT_VERSION`
  today.
- Code comments that reference the constant (`llm_adapter.py:292`, `:298`, `:327`,
  `migrations/005_summary_cache.sql:5-6`) are updated. Applied migrations are never edited, so
  005 stays as it is and 021's header comment supersedes it.
- `docs/adr-002-*.md:62` is a historical ADR and stays unchanged.
- The ADR-003 status becomes "Accepted and implemented", with the amendments dated.
