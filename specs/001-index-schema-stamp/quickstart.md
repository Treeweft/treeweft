# Quickstart: Validating the Index Stamp

These are the checks that prove the feature works. The API shapes are in
[contracts/http-api.md](contracts/http-api.md), and the states are in
[data-model.md](data-model.md).

## 1. Unit suite (no services)

```bash
env -u PYTHONPATH python -m pytest tests/unit -q
```

It must pass, including these new tests:

- `test_index_stamp_decision.py`: the decision table (fresh, match, each field mismatching,
  unreadable, a newer schema, legacy passed/failed/unavailable, a graph without vector data) and
  aggregation precedence.
- `test_index_guard.py`:
  - health fields in every state, with `status` always `ok`;
  - 409s on the read routes while `reindex_required`, and admin, auth and source routes still
    served;
  - index jobs refused while `unverified`;
  - the inline re-check that recovers once the fake embedder comes up;
  - the background retry.
- `test_index_gate_routes.py`: every read or write route calls its guard, and any new index
  route must be classified.
- `test_index_dispatch_gate.py`: a job reaching `dispatch_job` while refused ends `failed` and is
  not retried.
- `test_index_rebuild.py`:
  - the dry run lists sources and chunk counts and writes nothing;
  - the real rebuild drops, stamps, and enqueues one group with a preset `task_count`;
  - blockers give a 409 and nothing is dropped;
  - non-admins are refused;
  - `rebuilding` progress, then `ok`;
  - an interrupted rebuild gives `reindex_required`;
  - the summary cache is untouched.
- `test_sqlite_index_stamp.py`, `test_lancedb_index_stamp.py`, `test_chromadb_index_stamp.py`:
  real embedded stores on a temp dir. They cover the stamp round-trip, stamp on create, a
  metadata merge that keeps unrelated keys, `clear_*` sparing the stamp, and the dimension read
  from the schema.
- `test_legacy_verification.py`: the ≥ 0.99 threshold, `embed()` used rather than
  `embed_query()`, truncated rows skipped, fewer than 3 rows, 0 rows.
- `test_contracts.py`: the index-breaking classification table, and the committed
  `contracts/index_schema.json` matching the code.
- `test_mcp_compat.py`: a 409 with the reindex `detail` reaches the agent as
  `"Indexer returned HTTP 409: Index requires rebuild: …"`.

Regression proof (constitution II): with the gate calls removed, `test_index_guard.py` and
`test_index_gate_routes.py` must fail. Record that in the PR.

## 2. Integration tests (opt-in, real services)

```bash
MILVUS_TEST_URI=http://localhost:19530 NEO4J_TEST_URI=bolt://localhost:7687 \
  env -u PYTHONPATH python -m pytest tests/integration -m slow -k index_stamp -q
```

These check the Milvus property round-trip and dimension read, and the Neo4j meta node
round-trip, all under unique names that are cleaned up afterwards.

## 3. UI

```bash
cd ui && npm test && npm run build
```

`HealthIndicator.test.tsx` covers every index state, and the build must type-check. Manual check: with the indexer in `reindex_required`, the header
health indicator shows "Re-index required", and hovering shows the reason.

## 4. End-to-end on the live stack (ADR-004 §6)

Before starting, confirm the served embedding model and the reranker are the intended ones
(CLAUDE.md invariant).

1. Start the indexer on this branch. `curl -s localhost:8001/health | jq` shows
   `index_schema: 1` and `index_status: ok`. An existing index was adopted: the log shows
   "adopted … cosine=[…]".
2. Simulate a model change by editing the Milvus property:
   `alter_collection_properties("treeweft_chunks", {"treeweft.embedding_model": "fake/model"})`.
   Then restart the indexer.
3. `/health` shows `reindex_required`, with a reason naming the Milvus `embedding_model` and both
   values. A `POST /search` returns 409 with that `detail`. An MCP `search_code` call returns
   `"Indexer returned HTTP 409: …"`. `/sources` and sign-in still work.
4. `POST /index/rebuild?dry_run=true` as an admin lists every source and its chunk counts.
   Nothing changes: the collection row count is the same.
5. `POST /index/rebuild` returns 202 and a `group_id`. `/health` shows `rebuilding` with
   `rebuild_progress`. A search returns results for sources already rebuilt.
6. When the group completes, `/health` shows `ok`, and a search returns results across all
   sources.

Simple mode (optional): repeat steps 1–3 with `TREEWEFT_PROFILE=simple`, changing
`EMBEDDING_MODEL` in the environment instead of editing the store.
