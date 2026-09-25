# Implementation Plan: Index Schema Stamp, Reindex-Required Mode and Rebuild

**Branch**: `001-index-schema-stamp` | **Date**: 2026-09-25 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-index-schema-stamp/spec.md`; governing record
[ADR-004](../../docs/adr-004-compatibility-versioning.md) §3 (with §4–§6 as they apply).

## Summary

The work splits into five parts:

1. **Stamp the stores.** Every vector store (Milvus, LanceDB, ChromaDB) and graph store (Neo4j,
   SQLite) records what built its data: `INDEX_SCHEMA_VERSION`, the embedding model and the
   dimension.
2. **Check at startup.** A pure decision table in `domain/index_stamp.py` compares the stamps
   before any job can run, and `application/index_guard.py` holds the result. The indexer adopts
   pre-1.0.0 data after re-embedding up to three chunks, or reports `unverified` and retries every
   60 s and before each index job.
3. **Report and gate.** `/health` reports the status. While `reindex_required`, the read routes
   return a 409 whose `detail` survives the MCP's 300-character forwarding. Index jobs are refused
   at the route and again at `dispatch_job`.
4. **Rebuild.** `POST /index/rebuild` (admin; `dry_run`) creates a preset `index-rebuild` job
   group, drops and recreates the stores stamped, and re-enqueues every source. Its progress drives
   `rebuilding`.
5. **Guard future schema changes.** `contracts/index_schema.json` plus an `index-breaking`
   classifier stop schema drift in later releases.

**Several indexer processes are supported** (research R13):

- The stamps and the rebuild job group are shared state.
- One Postgres advisory maintenance lock coordinates rebuilds, community builds and stamp
  writes.
- Each process caches its view and refreshes it every 5 s.
- Workers mark a job `running` before an authoritative `dispatch_allowed()` check, so no job
  outside the rebuild can write while the stores are recreated.

The PR bumps `pyproject.toml` to 1.1.0, because the new endpoint is additive and the contract test
requires the bump.

## Technical Context

**Language/Version**: Python ≥ 3.11 (CI's `PYTHON_VERSION`); TypeScript / React 19 for the UI
indicator.

**Primary Dependencies**:

- Backend: FastAPI ≥ 0.115, pymilvus 3.0.0, lancedb 0.33.0 / pyarrow 24.0.0, chromadb 1.5.9,
  neo4j async driver 6.x, aiosqlite, asyncpg (job stores).
- UI: TanStack Query, vitest.

**Storage**:

- Stamps live in each vector and graph store (research R1).
- Rebuild state reuses the Postgres `job_groups` and `jobs` tables. No migration.
- Cross-process coordination uses a Postgres session-level advisory lock, which needs no table
  (research R13).

**Testing**:

- Unit: pytest, Detroit-style fakes; embedded LanceDB, Chroma and SQLite on temp dirs.
- Integration: opt-in `slow` tests for Milvus and Neo4j.
- UI: vitest plus the `tsc` build.

**Target Platform**: Linux host indexer (port 8001); Docker for the MCP server and UI; full stack
and simple mode.

**Project Type**: Web service (indexer API) plus MCP proxy plus web UI, in one repository.

**Performance Goals**:

- `/health` and the route gates answer from the per-process cache and never touch stores or
  Postgres.
- Cross-process staleness is at most `INDEX_STATUS_REFRESH_SECONDS` (5 s).
- `dispatch_allowed()` costs one Postgres probe per job.
- The startup check costs at most three embeddings, and only for unstamped data.
- Rebuild-group progress is cached for 5 s.

**Constraints**:

- No adapter imports on the startup path; the shims are used instead.
- `status` never changes, so health checks don't flap.
- No job may write before the check completes.
- Several indexer processes must work (R13). The maintenance lock is held on a dedicated
  connection because the pool `max_size` is 5.
- Legacy verification is time-bounded (`INDEX_VERIFY_TIMEOUT_SECONDS`, default 15).

**Scale/Scope**: Fleets of tens to hundreds of sources, and collections of millions of chunks.
Rebuild re-uses the fleet queue and is not parallelised beyond it.

No open clarifications. Research R1–R12 resolved every unknown.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design. It passes both times.*

| Principle | How this plan complies |
|---|---|
| I. Verification first | `quickstart.md` fixes the checks: the unit suite, opt-in integration tests, the UI build and vitest, and the ADR-004 §6 live end-to-end run. The PR reports each of them, including any skipped. |
| II. Test-first, taxonomy | Every behaviour lands with a test written first. Unit tests use fakes, or embedded stores on temp dirs (LanceDB, Chroma, SQLite are in-process, which is allowed). Milvus and Neo4j tests live in `tests/integration`, marked `slow`, opt-in by env, with unique names and cleanup. The gate's regression test is shown failing with the gate calls removed. |
| III. Evidence-gated retrieval | No ranking, payload or model change. Gating only refuses or allows requests. No benchmark needed. The live end-to-end step still verifies the served models first. |
| IV. Layering | The decision logic lives in `domain/index_stamp.py` with no adapter imports. The adapters implement the new functions behind the `retriever` and `graph_store` shims. `contracts.py` imports the adapters lazily. The MCP server is unchanged, apart from a test that it already forwards the 409. Simple mode is covered, and `test_simple_profile.py` still guards it. |
| V. Fail loud | This is the feature's purpose. An unreadable stamp counts as a mismatch, never as "no stamp". An interrupted rebuild shows as `reindex_required`, never an empty `ok`. A dispatch-level gate catches jobs the routes miss. Every adoption logs its cosine scores. |
| VI. Secure by default | The rebuild uses `_require_admin`. Gates run after authorization, so they disclose nothing beyond the public `/health`. `reindex_reason` exposes model names and store kinds on a public endpoint. ADR-004 requires this, and it holds no secrets or paths. |
| VII. Versioned data | This implements the index-stamp clause. `INDEX_SCHEMA_VERSION` stays 1: the only structural additions are the meta node and table, which invalidate no data (R9). The version is bumped to 1.1.0 (MINOR) because of the new endpoint and fields. The transition note is updated per Plan 1's hand-off. |
| Workflow gates | ADR-004 is not amended. LanceDB field metadata is table schema metadata, the placement ADR-004 left to planning. The FR-011 retry extends the ADR without conflicting with it, by the maintainer's decision. ChromaDB, which ADR-004's table omits, gets a stamp under spec FR-002. Docs are updated in the same PR. |

## Project Structure

### Documentation (this feature)

```text
specs/001-index-schema-stamp/
├── plan.md              # this file
├── research.md          # Phase 0: R1–R12 decisions
├── data-model.md        # Phase 1: stamp, observation, decision table, status, transitions
├── quickstart.md        # Phase 1: validation guide
├── contracts/
│   ├── http-api.md      # /health fields, 409 routes, POST /index/rebuild
│   ├── errors.md        # 409 body
│   └── store-ports.md   # functions added behind the retriever / graph_store shims
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks, not created here)
```

### Source Code (repository root)

```text
src/treeweft/
├── versions.py                         # + INDEX_SCHEMA_VERSION = 1
├── domain/index_stamp.py               # NEW: IndexStamp, StoreObservation, decide_store, aggregate, reason text
├── application/
│   ├── index_guard.py                  # NEW: cached status, run_check, refresh loop, legacy verification,
│   │                                   #      require_searchable/require_writable, writes_allowed,
│   │                                   #      rebuild (dry run + real), rebuild-status derivation
│   ├── lifecycle.py                    # ensure_schema → embedding proxy → JobStore/JobGroupStore init → run check
│   │                                   #   → queue start → recovery; start/stop refresh loop
│   ├── indexer_service.py              # /health fields; guard calls on read + write routes; POST /index/rebuild
│   ├── routes_webhook.py               # require_writable on job-enqueuing routes
│   └── (adapters/queue/postgres_queue.py) # _run_one: persist running → dispatch_allowed() → else failed, not retried
├── retriever.py                        # export observe_index, write_stamp, sample_chunks, drop_index
├── graph_store.py                      # _EXPORTED += observe_index, read_stamp, write_stamp, clear_index_data
├── adapters/
│   ├── milvus/vector_store.py          # build_collection_schema(dim), INDEX_PARAMS; stamp via properties;
│   │                                   #   init_collection stamps on create
│   ├── lancedb/vector_store.py         # _schema(dim), FTS_COLUMN; stamp via vector field metadata
│   ├── chromadb/vector_store.py        # stamp via collection metadata (merge)
│   ├── neo4j/graph_store.py            # TreeweftMeta node + constraint; batched clear sparing meta;
│   │                                   #   clear_all spares meta
│   ├── sqlite/graph_store.py           # treeweft_meta table; stamp read/write
│   └── postgresql/
│       ├── maintenance_lock.py         # NEW: advisory lock on a dedicated connection; pg_locks probe
│       └── job_group_store.py          # + latest_by_kind, delete
└── infrastructure/contracts.py         # index_schema_surface(), diff_index, "index-breaking", index snapshot I/O

scripts/update_contracts.py             # + index_schema snapshot
contracts/index_schema.json             # NEW baseline (schema 1, source_version 1.0.0)
ui/src/components/layout/HealthIndicator.tsx (+ .test.tsx)
pyproject.toml / uv.lock                # 1.0.0 → 1.1.0
CHANGELOG.md                            # Unreleased: Added (index stamp, rebuild), Operator notes
docs/upgrading.md                       # index_status, reindex_required, unverified, rebuild (dry run first), TEI caveat
docs/engineering-notes.md               # index states, rebuild, INDEX_VERIFY_* settings
docs/adr-004-compatibility-versioning.md # status: §3 implemented
docs/adr-003-prompt-versioning.md       # note: summary-prompt changes never bump INDEX_SCHEMA_VERSION
.specify/memory/constitution.md         # VII transition note updated (PATCH version bump)
CLAUDE.md                               # invariant: new index routes must call the guard; correct summary_vector "nullable" claim
.env.example                            # INDEX_VERIFY_INTERVAL_SECONDS, INDEX_VERIFY_TIMEOUT_SECONDS (commented defaults)

tests/unit/
├── test_index_stamp_decision.py        # NEW
├── test_index_guard.py                 # NEW
├── test_index_gate_routes.py           # NEW
├── test_index_dispatch_gate.py         # NEW
├── test_index_rebuild.py               # NEW
├── test_legacy_verification.py         # NEW
├── test_sqlite_index_stamp.py          # NEW
├── test_lancedb_index_stamp.py         # NEW
├── test_chromadb_index_stamp.py        # NEW
├── test_store_shim_exports.py          # NEW: every backend defines every exported stamp function
├── test_contracts.py                   # + index-breaking cases, index snapshot check
├── test_mcp_compat.py                  # + reindex 409 passthrough
└── test_route_table.py                 # + /index/rebuild
tests/unit/test_index_multiprocess.py   # NEW: interleavings (R13) with a fake lock + job store
tests/integration/
├── test_index_stamp_milvus.py          # NEW (slow, MILVUS_TEST_URI)
├── test_index_stamp_neo4j.py           # NEW (slow, NEO4J_TEST_URI; scoped clear)
└── test_maintenance_lock_pg.py         # NEW (slow, POSTGRES_TEST_URL; two real connections)
```

**Structure Decision**: The existing single-repository layout is used as is: DDD layers under
`src/treeweft/`, the React UI under `ui/`, and tests split into `tests/unit` and
`tests/integration`. No new packages.

## Suggested delivery order (for `/speckit-tasks`)

The order follows the spec's story priorities.

1. **Foundation**:
   - `INDEX_SCHEMA_VERSION`;
   - the `domain/index_stamp.py` decision table and its tests;
   - the store functions for SQLite and LanceDB first, because they are unit-testable end to
     end, then Chroma, Milvus and Neo4j;
   - the maintenance lock, and `JobGroupStore.latest_by_kind`/`delete`;
   - the shim exports test.
2. **US1 and US2 (P1)**:
   - `index_guard` check plus legacy verification;
   - the lifecycle ordering;
   - `/health` fields;
   - the read and write gates with the route-table test;
   - the dispatch gate (persist `running`, then `dispatch_allowed()`);
   - the per-process refresh loop and the verification retry;
   - the MCP 409 test.
3. **US3 (P2)**:
   - the rebuild dry run;
   - the real rebuild;
   - the maintenance lock, the community-build shared lock, and the cross-process
     interleaving tests (R13);
   - status derivation in `refresh()`, including `preparing` and an interrupted rebuild.
4. **US4 (P3)**:
   - the schema-builder refactors;
   - `index_schema_surface`, the classifier and the baseline snapshot;
   - the update script.
5. **Cross-cutting**:
   - the UI indicator;
   - integration tests;
   - the version bump and CHANGELOG;
   - docs, ADR status, ADR-003 note, constitution note, CLAUDE.md invariant.
6. **Verification**: `quickstart.md` §1–§4, reported in the PR.

## Complexity Tracking

No constitution violations. Two scope notes are recorded for reviewers:

| Item | Why | Simpler alternative rejected because |
|---|---|---|
| ChromaDB stamped although ADR-004 §3 omits it | `VECTOR_STORE=chromadb` is selectable; FR-002 covers every store | Leaving it unstamped would let Chroma deployments search a mismatched index silently |
| Postgres advisory maintenance lock plus a per-process refresh loop | Several indexer processes are required (maintainer decision). In-memory state alone cannot coordinate a rebuild across processes | Pinning to one process was rejected. A lock row in a table needs a migration and cannot detect a dead holder; an advisory lock is released when its holder's connection dies |
| Two-layer gate (route and dispatch) | Fleet auto-refresh, restart recovery and pre-queued jobs bypass the routes | A route-only gate misses those paths; a dispatch-only gate would return 202 and then fail the job, which is worse feedback |
