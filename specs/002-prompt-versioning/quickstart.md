# Quickstart: Validating Prompt Versioning

These are the checks that prove the feature works. The API shapes are in
[contracts/http-api.md](contracts/http-api.md), and the states are in
[data-model.md](data-model.md).

## 1. Unit suite (no services)

```bash
env -u PYTHONPATH python -m pytest tests/unit -q
```

It must pass, including these new tests:

- `test_prompt_registry.py`:
  - the hash fixture: an edited version fails, a version without a hash fails, and a fixture
    entry for an unregistered version fails;
  - v3 and v1 render byte for byte as the pre-change prompts, with and without the no-think
    suffix;
  - the registered schemas are frozen.
- `test_prompt_pins_domain.py`: resolution precedence (an override beats the deployment pin), the
  staleness rule including a set refresh target, the seeding rule (fresh, existing sources, all
  NULL, ties), and the no-Postgres baseline.
- `test_prompt_pins_sync.py`, against a fake pool:
  - `load_and_seed` seeds once under a simulated race;
  - a missing table raises and names migration 021;
  - an unknown stored pin raises and names the valid versions;
  - a reload after reconnect picks up a missed change;
  - the periodic reload converges within `PROMPT_PINS_REFRESH_SECONDS`;
  - a HyDE pin change clears `_HYDE_CACHE`.
- `test_prompt_routes.py`:
  - admin-only on every route;
  - 400 with `valid_versions`, for dry-run and real calls;
  - a HyDE override gives 400;
  - 503 without Postgres;
  - **the dry run equals the real call's plan**, across the table from spec SC-004 (a deployment
    change skips overridden sources; already-current sources are excluded; deferred and
    not-enqueued sources are listed);
  - a dry run writes no row, sends no `NOTIFY` and creates no job;
  - `updated_by` is recorded;
  - deleting a source deletes its override.
- `test_summary_versioning.py`:
  - `cache_put` and `cache_get_many` use the resolved version, not a constant;
  - the HyDE cache key includes the version;
  - `summarize_with_cache` returns the strategy;
  - the summary tail reads at the recorded version, then the effective version;
  - an unregistered read override (9002) still works.
- `test_resummarize_job.py`, against a fake vector store:
  - it takes a snapshot, then processes batches;
  - the store's no-summary value is used;
  - the mark is set before the first write;
  - a clean run advances the version and clears the mark;
  - a transient error leaves both columns unchanged;
  - a re-run makes no LLM calls for cached chunks;
  - a count mismatch fails the job;
  - a deleted source ends the job;
  - a pin that moved while the job was queued is re-resolved;
  - the job is a no-op when summary vectors are disabled or the store is ChromaDB;
  - the worker refuses it while the index is not writable.
- `test_full_index_summary_version.py`:
  - a clean file, directory or repo job records its version and clears the mark;
  - a job with a transient summary error changes neither column;
  - graph and incremental jobs change nothing;
  - with summary vectors off, the version becomes NULL;
  - a resumed job keeps its payload version.
- `test_refresh_hooks.py`:
  - a finished non-refresh job enqueues a refresh for a stale source;
  - the hook never runs after a `resummarize` job;
  - startup recovery re-enqueues an interrupted `resummarize` instead of marking it superseded.
- `test_lancedb_summary_rewrite.py`, on a real LanceDB temp table:
  - `merge_insert` updates `summary_vector` in place;
  - IDs, row count, `vector`, text and FTS results are unchanged;
  - None stays NULL.
- `test_store_shim_exports.py`: the five new functions exist for milvus, lancedb and chromadb.

Regression proofs (constitution II), each shown failing without its fix and recorded in the PR:

- the incremental-job rule (FR-010);
- the refresh-target rule (FR-015);
- the dry-run write guard.

## 2. UI

```bash
cd ui && npm test && npm run build
```

- `PromptsPage.test.tsx`:
  - it renders versions, notes, pins, and per-source built/target/stale;
  - choosing a version calls the dry run first and shows its result;
  - Cancel sends nothing;
  - Confirm sends the real PUT and shows the enqueued jobs;
  - 401 or 403 renders "Admin access required".
- `prompts.test.ts`: the pure helpers.

## 3. Integration tests (opt-in, real services)

```bash
MILVUS_TEST_URI=http://<milvus>:19530 POSTGRES_TEST_URL=postgresql://…@10.16.1.226:5432/<test db> \
  env -u PYTHONPATH python -m pytest tests/integration -m slow -k "resummarize or prompt_pins" -q
```

- `test_resummarize_milvus.py`, on a per-run collection `itest_resum_<hex>` with the production
  schema, dropped before and after. After a rewrite:
  - the row count is unchanged and there are no duplicate `(file_path, start_line)` pairs;
  - the new `summary_vector` values are present;
  - `vector`, `chunk_text` and any dynamic field are unchanged;
  - both dense and BM25 search still return the chunks;
  - `query_iterator` honours `consistency_level="Strong"` (research R10).
- `test_prompt_pins_pg.py`, which uses only uniquely prefixed pin scopes:
  - `NOTIFY` reaches a second connection;
  - the `CHECK` rejects a HyDE override;
  - a seed race between two connections leaves one row.

If the homelab hosts are unreachable, say so and ask. Do not work around it.

## 4. Live end-to-end (ADR-003 §5)

First, verify the served LLM, embedding model and reranker, and where they run, from the live
endpoints (constitution III). Configuration files are not evidence. Record them.

1. Take a clean upgrade on an existing index. Save the top-10 `file_path`s for 3 fixed queries.
   Start the new build and check:
   - `GET /prompt-versions` shows the pins seeded to `chunk_summary` 3 and `hyde` 1;
   - every source is at `summary_prompt_version` 3, and none is stale;
   - no LLM summary calls happen (LLM request metrics are flat);
   - the 3 queries return identical results (SC-001).
2. Add a local, **uncommitted** `chunk_summary` v4 to `prompts.py` (a small wording change, plus
   its fixture hash) and restart. Pins do not move and nothing becomes stale.
3. Index a small test source. It records v3.
4. Run `PUT /prompt-pins/chunk_summary/sources/<test source>?dry_run=true` with `{"version": 4}`.
   The response lists that source and its chunk count, and nothing changes. Then run the real
   call.
5. Watch the `resummarize` job to `done`. Record:
   - that parse and code-embedding counters did not move, and summary-embedding and LLM counters
     did (SC-002);
   - the row count before and after;
   - that code `vector` values are equal for 5 sampled chunks, and `summary_vector` values
     differ.
6. Confirm the source is current at v4. Clear the override, run the dry run, then the real call.
   The source goes back to v3 and is refreshed. Its summaries are cache hits, so there are about 0
   LLM calls (SC-003).
7. Interrupt a refresh (stop the indexer mid-job) and restart. The source is still stale, with
   `summary_refresh_target` set, and the job is re-enqueued (FR-015, R5).
8. With two indexer processes: change the HyDE pin through one. Within 5 s both report the new
   pin, and a repeated query on the other process is not served from the previous version's cache
   (SC-008).
9. Set an override of v4 on the test source again, then remove the local v4 and restart. Startup
   must **fail loudly**, naming the pin and the registered versions (constitution V). Put v4 back,
   clear the override, remove v4 again and restart. Startup succeeds, and `git status` shows
   `prompts.py` and the fixture clean.

Record each step's HTTP codes and observed values for the PR, including any step that could not
be run and why.
