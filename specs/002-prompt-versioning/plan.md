# Implementation Plan: Prompt Versioning with Admin Pins and Summary-Only Refresh

**Branch**: `002-prompt-versioning` | **Date**: 2026-09-25 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/002-prompt-versioning/spec.md`; governing record
[ADR-003](../../docs/adr-003-prompt-versioning.md), amended in this PR where research found it
wrong (below).

## Summary

The work splits into six parts:

1. **Registry.** `adapters/llm_api/prompts.py` holds immutable `PromptVersion`s (`chunk_summary`
   v3 and `hyde` v1, byte-identical to today's), protected by a hash fixture. The prompt
   constants are deleted, and every summary and HyDE call takes an explicit version (research
   R1, R2).
2. **Pins.** Migration 021 adds `prompt_pins` and two `source_records` columns,
   `summary_prompt_version` and `summary_refresh_target`.
   - `application/prompt_pins.py` loads, seeds and validates pins at startup, failing loud on an
     unknown pin or a missing migration.
   - It keeps them in sync across processes: LISTEN, a reload on reconnect, and a 5 s poll.
   - The pure resolution and staleness rules live in `domain/prompt_pins.py` (R3, R9).
3. **Version bookkeeping.**
   - Only a *clean full* index job (no file errors, and no transient summary errors) or a clean
     refresh advances a source's recorded version. It uses the version resolved when the job
     started.
   - Incremental and graph jobs never advance it.
   - A refresh marks the source before its first write and clears the mark only on a clean
     finish (R6, FR-015).
4. **The `resummarize` job.**
   - It snapshots the source's row IDs at `Strong`, then works in batches: fetch full rows,
     summarize through the existing cached path, embed the summaries, and write back through a
     store-specific `write_summary_vectors`. Milvus uses a full-row upsert; LanceDB uses
     `merge_insert`. ChromaDB has no summary vectors, so it is a reported no-op.
   - A final count check at `Strong` guards against duplicates (R7, R10).
   - Refreshes that would collide with an active job are deferred and enqueued by a post-job hook.
     An interrupted refresh is re-enqueued at startup (R4, R5).
5. **Admin API and UI.** `routes_prompts.py` serves `GET /prompt-versions`, three pin mutations
   with `?dry_run=true`, and `POST /sources/{id}/resummarize`, all admin-only. A **Prompts** page
   adds an inline dry-run confirmation (R11, R14).
6. **Docs and release.**
   - Engineering notes, a new `prompt-versions.md` runbook, a `CLAUDE.md` invariant, and the
     ADR-003 status.
   - `CHANGELOG.md` gets an Unreleased entry. The version stays 1.1.0, because 1.1.0 is
     unreleased (R13).

**ADR-003 amendments in this PR** (constitution: the ADR is amended first where artifacts
conflict). Two are already made (§2): FR-010, incremental jobs; FR-015, the refresh target.
These are added with this plan:

- §3 "serializes" becomes defer-and-hook (R4);
- interrupted refreshes are re-enqueued at startup (R5);
- a clean full job must count summary errors (R6);
- ChromaDB is a no-op, and "the zero vector" becomes "the store's no-summary value" (R7);
- §1 propagation adds reload-on-reconnect and a poll fallback (R3);
- the no-Postgres baseline (R9);
- the summary-tail default read (R8).

**Spec corrections with this plan**:

- FR-031 and the assumption: the version stays 1.1.0 (R13);
- US4 scenario 4: the tab stays, and the page shows "Admin access required" (R14);
- the edge case "serializes" becomes deferred (R4);
- ChromaDB joins the no-op edge case (R7).

## Technical Context

**Language/Version**: Python ≥ 3.11 (CI also runs 3.14); TypeScript / React 19 for the UI.

**Primary Dependencies**:

- Backend: FastAPI, asyncpg, pymilvus 3.0.0 (against Milvus 2.5.4), lancedb 0.33.0 / pyarrow,
  chromadb 1.5.9.
- UI: TanStack Query 5, react-router 7, vitest with testing-library.
- No new dependencies.

**Storage**:

- Postgres: new table `prompt_pins`; `source_records` gains `summary_prompt_version` and
  `summary_refresh_target` (migration 021); the existing `summary_cache` is unchanged.
- The vector stores are rewritten in place: only `summary_vector` changes. Their schema is
  unchanged, and `INDEX_SCHEMA_VERSION` stays 1.

**Testing**:

- Unit: pytest with Detroit-style fakes (the fake asyncpg pool pattern; a fake vector store), and
  a real LanceDB temp table.
- Integration: opt-in, `slow`, Milvus and Postgres.
- UI: vitest plus `tsc` via `npm run build`.

**Target Platform**: Linux host indexer (port 8001), in the full stack and simple mode. Postgres
is present in both. Without `DATABASE_URL`, the baseline versions are used and the pin API
returns 503.

**Project Type**: Web service (indexer API) plus web UI. The MCP server is untouched.

**Performance Goals**:

- Resolution is an in-memory dict lookup: no database round-trip per chunk or query (FR-006).
- Pin convergence across processes is ≤ 5 s (SC-008).
- A refresh costs only LLM calls for summary-cache misses, plus summary embeddings and the
  write-back. There is no parsing, code embedding or graph work (SC-002).

**Constraints**:

- Startup-path code imports no backend adapter; the new store functions go through the
  `retriever` shim (constitution IV).
- Milvus upserts re-key rows, so the refresh never iterates live rows; it works from a snapshot.
- Migration SQL is idempotent (unlocked runner, issue #28), and the pin loader fails loud if 021
  is missing.
- The Postgres pool `max_size` is 5, and the LISTEN connection is dedicated.

**Scale/Scope**:

- Sources: tens to hundreds.
- Chunks: up to millions per collection, with a single source up to about 1M chunks. A snapshot
  of 1M chunks is about 8 MB of IDs.
- A deployment-pin change enqueues at most one refresh per source, as one job group.

No open clarifications: research R1–R15 resolved every unknown. R7's LanceDB partial-column
`merge_insert` is settled by a unit test on a real temp table, with a defined fallback.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design. It passes both times.*

| Principle | How this plan complies |
|---|---|
| I. Verification first | `quickstart.md` fixes the checks: the unit suite, UI tests and build, opt-in Milvus and Postgres integration tests, and the ADR-003 §5 live end-to-end run. The PR reports each, including anything skipped. |
| II. Test-first, taxonomy | Every behaviour lands with a test written first. The unit tests use fake pools and a fake vector store; LanceDB runs as a real temp table, which is allowed. The Milvus and Postgres tests are in `tests/integration`, `slow`, gated by environment variable, with unique names and cleanup. Regression proofs are named for FR-010, FR-015 and the dry-run write guard. |
| III. Evidence-gated retrieval | No prompt text changes, and v3 and v1 are byte-identical (a test proves it), so no retrieval default moves and no benchmark is needed. Registering a future version and making it a fresh install's default is a retrieval change that needs a benchmark (spec assumption). The live run verifies the served models first. |
| IV. Layering | Pure resolution and staleness live in `domain/prompt_pins.py`. The Postgres and vector-store I/O live in adapters behind the shims. The application wires routes, jobs and startup. The MCP server is unchanged. The simple profile is covered, and `test_simple_profile.py` still guards the import rule. |
| V. Fail loud | Startup aborts on an unknown pinned version or a missing 021. A refresh with transient errors never advances the version. A mixed-vector source stays stale (FR-015). The post-refresh count check fails loudly. No-op refreshes (disabled, ChromaDB) say why. Deferred and not-enqueued refreshes are reported, never dropped. |
| VI. Secure by default | Every new route uses `_require_admin`, and mutations record `updated_by`. The read endpoint is kept out of the `/sources` GET auth-skip. The Milvus snapshot filter is escaped with `_escape_literal`; the others are parameterized. The nonce data fence stays shared by every prompt version. The new mutating routes join the CSRF and admin-surface tests. |
| VII. Versioned data | The Postgres change ships as new migration 021. `INDEX_SCHEMA_VERSION` is unchanged and there is no `reindex_required`: summary-prompt changes are orthogonal, as the principle states. The API change is additive (MINOR); it stays 1.1.0 because 1.1.0 is unreleased (R13), and the contract check is satisfied. |
| Workflow gates | ADR-003 is amended in the same PR wherever these artifacts diverge (listed above). The docs are updated in the same PR (FR-029). ADR-003's status is updated (FR-030). |

## Project Structure

### Documentation (this feature)

```text
specs/002-prompt-versioning/
├── plan.md              # this file
├── research.md          # Phase 0: R1–R15
├── data-model.md        # Phase 1: registry, pins, source state machine, migration 021
├── quickstart.md        # Phase 1: validation guide
├── contracts/
│   ├── http-api.md      # /prompt-versions, /prompt-pins/*, /sources/{id}/resummarize
│   └── store-ports.md   # vector-store shim functions, summary path, pins, refresh orchestration
├── checklists/
│   └── requirements.md
└── tasks.md             # /speckit-tasks (not created here)
```

### Source code

```text
src/treeweft/
├── domain/prompt_pins.py                          # NEW pure resolve / is_stale / seed_version
├── adapters/
│   ├── llm_api/prompts.py                         # NEW registry
│   ├── llm_api/llm_adapter.py                     # versioned summary/HyDE/cache; constants removed
│   ├── llm_api/llm_caller.py                      # schemas move to the registry
│   ├── postgresql/migrations/021_prompt_versions.sql   # NEW
│   ├── postgresql/prompt_pin_store.py             # NEW
│   ├── sources/repository.py                      # mark_summary_refresh, record_summary_version, histogram
│   ├── milvus/vector_store.py                     # snapshot/fetch/write/count (adapter methods + wrappers)
│   ├── lancedb/vector_store.py                    # same, merge_insert
│   └── chromadb/vector_store.py                   # unsupported stubs
├── domain/sources/__init__.py                     # SourceRecord gains the two fields
├── retriever.py                                   # export the five functions for all backends
└── application/
    ├── prompt_pins.py                             # NEW view, load_and_seed, sync, set_pin/clear_override
    ├── prompt_refresh.py                          # NEW plan/enqueue/defer, post-job hook
    ├── routes_prompts.py                          # NEW admin router
    ├── indexer_runners.py                         # dispatch 'resummarize'; _summaries_for_chunks(version); _finalize_job rule
    ├── retrieval.py                               # summary-tail default read version
    ├── lifecycle.py                               # load_and_seed + start_sync; recovery exemption; stop_sync
    └── indexer_service.py                         # include router; /sources fields; delete override on source delete
src/treeweft/adapters/queue/postgres_queue.py      # worker: post-job hook call

tests/unit/          # the tests listed in quickstart §1, plus fixtures/prompt_hashes.json
tests/integration/   # test_resummarize_milvus.py, test_prompt_pins_pg.py

ui/src/
├── api/prompts.ts (+ prompts.test.ts)             # NEW
├── pages/PromptsPage.tsx (+ PromptsPage.test.tsx) # NEW
├── routes/router.tsx, components/layout/AppShell.tsx   # route + tab
└── pages/JobDetailPage.tsx, pages/JobsPage.tsx    # "chunks" unit for resummarize

docs/engineering-notes.md, docs/prompt-versions.md (NEW), docs/adr-003-prompt-versioning.md,
CLAUDE.md (invariant), CHANGELOG.md
```

**Structure Decision**: this is the existing single-repository layout (`src/treeweft` with the
DDD layers, `ui/`, `docs/`, `tests/unit` and `tests/integration`). The new code follows the
existing patterns:

- the embedding-backends registry, for pins and the listener;
- 001's shim functions, for the vector-store operations;
- `/index/rebuild`, for the dry run;
- BackendsPage, for the UI.

### Suggested delivery order (input to `/speckit-tasks`)

1. **Foundation**: registry plus hash test; versioned summary, HyDE and cache paths; migration
   021; pin store; domain rules; `load_and_seed` and sync; the full-job bookkeeping. This is
   **US1** and the MVP.
2. **US2**: the store functions (all three backends); the refresh job; the deferral hook and
   startup recovery; the deployment-pin API with dry run; the read endpoint.
3. **US3**: overrides (set/clear, both with dry run); manual refresh; delete-with-source.
4. **US4**: the UI.
5. **Polish**: docs, ADR status, CHANGELOG, the integration tests, and the live end-to-end run.

## Complexity Tracking

No constitution violations to justify.
