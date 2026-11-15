# Implementation Plan: Backend Credential Pre-flight and Fail-Fast Indexing

**Branch**: `005-backend-credential-preflight` | **Date**: 2026-11-15 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/005-backend-credential-preflight/spec.md`

## Summary

A wrong embeddings key let a 37,019-file job run for ten minutes at one failing call per file,
because `_walk_and_index` catches each per-file exception, bumps `job["errors"]`, and moves on; the
job only learns it produced nothing at the very end (`_terminal_status_for_index`). The plan adds
two defences that mirror the parser-readiness gate already in the codebase:

1. **Pre-flight** (`require_credentials()` beside `require_parsers()` on the gated routes, plus a
   dispatch-time check for jobs already queued): one cached, single-flight probe per distinct
   backend, classified `valid` / `auth_failed` / `unreachable`; only `auth_failed` refuses.
2. **Fail-fast** inside a running job: the embedding adapter raises a typed
   `BackendAuthError` on 401/403 (instead of the generic `HTTPStatusError`); the walk loop counts
   consecutive ones, and on reaching the threshold stops dispatching files and fails the job with
   an `embedding_auth_failed:` reason.

Status is exposed as role + state in `/health`. Nothing here changes retrieval or the index
schema.

## Technical Context

**Language/Version**: Python 3.11 (CI also runs 3.14)

**Primary Dependencies**: FastAPI/Starlette, `httpx` (embedding and LLM adapters already use it),
`pytest` + `pytest-asyncio`; no new dependency

**Storage**: none new. Probe state is per-process memory, like `parsers._cached`; job failure uses
the existing job persistence (`_persist_job`)

**Testing**: unit tests in `tests/unit/` with a fake `httpx` transport (no real provider);
ordering and cache behavior tested with an injected clock, as `test_parser_readiness.py` does. One
opt-in integration test is not needed: no new real service is touched

**Target Platform**: the indexer service (both profiles); the stdio MCP server only surfaces text

**Project Type**: web service (single project); touches `adapters/`, `application/`,
`infrastructure/config.py`, docs and contracts; `domain/` gets the pure classification type

**Performance Goals**: pre-flight adds at most one probe per backend per check window (default
300 s); concurrent callers share it; refusal path answers well under 5 s (SC-001)

**Constraints**: provider-neutral (Principle IV); credential never in any output (FR-012); the push
webhook is never refused (FR-005)

**Scale/Scope**: 6 gated routes + dispatch, 1 adapter exception, 1 loop change, `/health`, docs,
~4 new settings

## Constitution Check

*Gate: must pass before research; re-checked after design.*

| Principle | Assessment |
|---|---|
| I. Verification First | PASS. Verification commands are in [quickstart.md](quickstart.md): the unit tests plus a manual run against a deliberately wrong key |
| II. Test-First, taxonomy | PASS. All tests are unit tests with a fake transport; a regression test reproduces the incident (401 per file) and must fail before the fix |
| III. Evidence-Gated Retrieval | N/A. No retrieval or ranking change |
| IV. Layered Architecture | PASS with care. The probe lives in `adapters/` (it speaks HTTP); classification and cache policy in `domain/` as pure code; the gate in `application/index_guard.py`. Startup path imports no new service-dependent adapter. Provider-neutral: no vendor name in logic, only the OpenAI-compatible wire shape both adapters already assume |
| V. Fail Loud | PASS. This feature is an application of it. The new refusals and the fail-fast stop are logged once per state change and each has a test showing when it engages (FR-013) |
| VI. Secure by Default | PASS with one design choice: `/health` is public, so it carries role and state only; host, model and reason text appear only on authenticated index routes and in the job error. Credential scrubbing is a tested invariant |
| VII. Versioned Data and Releases | PASS. Additive `/health` fields and a new 409 reason are a SemVer MINOR; `contracts/api.json` snapshot is regenerated; no index-schema, vector or Postgres change |

No violations; Complexity Tracking is empty.

**Post-design re-check (after Phase 1)**: unchanged. The data model adds no persisted entity and
the contracts are additive.

## Project Structure

### Documentation (this feature)

```text
specs/005-backend-credential-preflight/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── health-credentials.md
│   └── refusal-409.md
├── checklists/requirements.md
└── tasks.md            # /speckit-tasks, not created here
```

### Source Code (repository root)

```text
src/treeweft/
├── domain/
│   └── credentials.py                 # NEW: CredentialState, BackendStatus, streak counter, scrub()
├── adapters/
│   ├── openai_api/
│   │   ├── embedding_adapter.py       # CHANGED: raise BackendAuthError on 401/403; probe()
│   │   └── credential_probe.py        # NEW: one-input probes (embeddings, chat), classification
│   └── llm_api/llm_adapter.py         # CHANGED: probe hook for the summarizer (when enabled)
├── application/
│   ├── index_guard.py                 # CHANGED: require_credentials(), health fields, cache
│   ├── indexer_service.py             # CHANGED: call the gate on the 6 routes (not the webhook)
│   ├── indexer_runners.py             # CHANGED: dispatch-time check; streak + abort in the walk
│   └── routes_webhook.py              # UNCHANGED behavior (documented non-gating)
├── infrastructure/config.py           # CHANGED: 3 settings (pass TTL, recheck, streak)
docs/                                  # CHANGED: simple-mode.md, configuration reference, ADR note
contracts/api.json                     # REGENERATED: scripts/update_contracts.py
tests/unit/
├── test_credential_classification.py  # NEW
├── test_credential_preflight.py       # NEW: cache, single-flight, dedupe, refusal body, scrub
├── test_credential_failfast.py        # NEW: incident regression, streak reset, abort, retry
└── test_health_credentials.py         # NEW (or extend test_health_versions.py)
```

**Structure Decision**: single project, existing DDD layout. The pure state machine and scrub live
in `domain/` so they can be unit tested with no I/O; HTTP stays in `adapters/`; policy and wiring
stay in `application/`.

## Complexity Tracking

None.
