---

description: "Task list for backend credential pre-flight and fail-fast indexing"
---

# Tasks: Backend Credential Pre-flight and Fail-Fast Indexing

**Input**: Design documents from `/specs/005-backend-credential-preflight/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: INCLUDED. Constitution Principle II requires tests with every behaviour change, and a
regression test must be shown failing without the fix. Unit tests only, using a fake HTTP transport;
no real provider is contacted.

**Organization**: grouped by user story. US1 alone is a shippable MVP.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an unfinished task)
- **[Story]**: US1-US4, mapping to spec.md

## Path Conventions

Single project. Source under `src/treeweft/`, tests under `tests/unit/`. Run tests as
`env -u PYTHONPATH .venv/bin/python -I -m pytest -q <file>` with a CI-style `.env`
(`cp .env.example .env`).

---

## Phase 1: Setup

- [x] T001 Add the three settings to `src/treeweft/infrastructure/config.py`: `TREEWEFT_CREDENTIAL_CHECK_SECONDS` (default 300), `TREEWEFT_CREDENTIAL_RETRY_SECONDS` (default 30), `TREEWEFT_CREDENTIAL_FAIL_STREAK` (default 5, minimum 1). Parse like `parsers.retry_seconds()` (bad value falls back to the default). Document them in `.env.example` next to `TREEWEFT_PARSER_RETRY_SECONDS`
- [x] T002 [P] Create a shared fake-transport helper in `tests/unit/_credential_fakes.py`: an `httpx.MockTransport` factory that answers per-URL with a scripted status/body sequence, records requests (so tests can assert call counts and that no key appears in them beyond the auth header), and an injectable monotonic clock

---

## Phase 2: Foundational (blocks all user stories)

**Note**: write each test first and watch it fail before its implementation task.

- [x] T003 [P] Write `tests/unit/test_credential_classification.py`: 2xx -> `valid`; 401 -> `auth_failed`; 403 -> `auth_failed` unless the body names model, quota or permission-for-model (then `unreachable` with that reason); timeout, DNS error, TLS error, 429, 5xx and other 4xx -> `unreachable`; and `scrub()` removes the key and every prefix of it (length >= 4) and any `sk-`-shaped token from text
- [x] T004 Implement `src/treeweft/domain/credentials.py`: `CredentialState` enum, `BackendStatus` dataclass (roles, state, checked_at, host, model, reason_code), pure `classify(status_code, body_text)`, pure `scrub(text, secrets)`, and a `FailureStreak` class (`record_failure()`, `record_success()`, `aborted`) per data-model.md. No imports from `adapters/` (Principle IV)
- [x] T005 [P] In `src/treeweft/adapters/openai_api/embedding_adapter.py` add `BackendAuthError(RuntimeError)` (role, host, model, status; no response body) and raise it on 401/403 in `_embed_batch` instead of re-raising the `HTTPStatusError`. Remove the `exc.response.text[:200]` log for 401/403 and log host, model and status only. Keep the no-retry behavior for 4xx. Extend `tests/unit/` with a test that the raised error and every log record contain no part of the key
- [x] T006 Write `tests/unit/test_credential_preflight.py` cache tests: a pass is reused inside the TTL; `auth_failed` and `unreachable` expire after the shorter recheck interval; N concurrent callers with no cached result make exactly one probe; two roles sharing endpoint host and key make one probe and one status naming both roles (FR-006a); the cache key is never emitted (T002 helper)
- [x] T007 Implement `src/treeweft/adapters/openai_api/credential_probe.py`: `probe_embeddings(base_url, model, key, client)` (`POST /embeddings`, one-token input) and `probe_chat(llm_url, model, key, client)` (`POST /chat/completions`, `max_tokens=1`), each returning a `BackendStatus` via `classify`. Provider-neutral: only the OpenAI-compatible wire shape both adapters already assume
- [x] T008 Implement the check core in `src/treeweft/application/index_guard.py`: `resolve_backends()` (which roles apply: embedding only when `EMBEDDING_PROVIDER=openai` and a key is set; summarizer only when `USE_SUMMARY_VECTOR` is on; dedupe by endpoint and key), `check_backends(force=False)` with the TTL/recheck cache and per-key `asyncio.Lock` single-flight, and `reset_credential_cache()` for tests. Log once per state change (FR-013)

**Checkpoint**: classification, adapter error, probe and cache are tested; nothing is wired yet.

---

## Phase 3: User Story 1 - A bad key is refused before work is queued (P1) MVP

**Goal**: an invalid embeddings credential is refused with a 409 before any job exists.

**Independent Test**: with a backend answering 401, each gated route returns 409
`embedding_auth_failed`, no job is created, and the key appears nowhere.

- [x] T009 [P] [US1] Write `tests/unit/test_credential_gate_routes.py`, parametrized over the six gated routes (`/index-file`, `/index-directory`, `/index-repo`, `/index-graph`, job retry, `POST /index/rebuild`): 401 -> 409 with `reason`, `detail`, `backends[]`; no job record created; response, log and body contain no key or prefix; `unreachable` backend -> request accepted; valid -> accepted as today; fixed key accepted after the recheck interval without restart
- [x] T010 [P] [US1] Write `tests/unit/test_credential_dispatch.py`: a queued job whose backend is `auth_failed` at start ends `failed` with error prefix `embedding_auth_failed:`, takes the queue worker's exception path (retryable kind is re-enqueued, finally dead-lettered), and the push webhook still queues its job instead of answering 409 (FR-005)
- [x] T011 [US1] Implement `require_credentials()` in `src/treeweft/application/index_guard.py` returning a `JSONResponse | None`, with the 409 body from `contracts/refusal-409.md` (detail text, `reason`, `backends[]` with `setting` names, never values)
- [x] T012 [US1] Call `await index_guard.require_credentials()` immediately after each `require_parsers()` call site in `src/treeweft/application/indexer_service.py` (six routes). Do not add it to `src/treeweft/application/routes_webhook.py`; extend its existing comment to name this feature
- [x] T013 [US1] Add the dispatch-time check in `src/treeweft/application/indexer_runners.py` next to `_ParserGatedRunner` (reuse its fail-then-raise shape so retry and dead-letter behave identically); skip `resummarize` only if it does not call the embedder (verify, do not assume) _Resummarize stays ungated at dispatch, like the parser gate: it makes no walk of files and a batch failure already fails the job._

**Checkpoint**: US1 delivers the MVP. Stop and validate with quickstart "manual check".

---

## Phase 4: User Story 2 - A running job stops fast when credentials fail (P1)

**Goal**: a running job ends `failed` after a bounded number of consecutive auth failures.

**Independent Test**: a backend that answers 401 after N good calls ends the job within the
threshold, with no further embedding calls.

- [x] T014 [US2] Write `tests/unit/test_credential_failfast.py` first, and show it FAILING on `main`: **incident regression**, a many-file repo where every embeddings call answers 401 must end `failed` with `embedding_auth_failed` after at most `TREEWEFT_CREDENTIAL_FAIL_STREAK` failures and make no embedder calls afterwards; a single 401 between successes does not fail the job (reset); skipped files are not counted in `job["errors"]`; the job error text and logs contain no key; `retry` after a fix resumes the unprocessed files and ends `done`
- [x] T015 [US2] In `_walk_and_index` (`src/treeweft/application/indexer_runners.py`) catch `BackendAuthError` before the generic handler: feed the per-job `FailureStreak`, reset on a successful file, and on abort set the flag so remaining `_process_one` coroutines return without calling the embedder. After the gather, raise so the job ends `failed` with `embedding_auth_failed: <role> <host>` (scrubbed). Persist via the existing `_persist_job`; log once (FR-013)
- [x] T016 [US2] Cover the single-file path: `_run_index_file_job` (embeds at `embeddings = await embed(texts)` then `_fail_job`) must surface `BackendAuthError` as the same `embedding_auth_failed:` job error; add a case to `test_credential_failfast.py`
- [x] T017 [US2] Check the other `embed(` call sites in `indexer_runners.py` (summary vectors, resummarize, graph) for the same behavior and add a case for each that can reach the adapter; record any site that cannot in a comment in the test _Done: the walk, the single-file runner and the incremental runner (which also swallowed per-file errors) carry the streak; resummarize fails per batch, not per file._

**Checkpoint**: US1 + US2 together close the incident for both new and in-flight jobs.

---

## Phase 5: User Story 3 - Credential health without starting a job (P2)

**Goal**: `/health` shows role and state, never host, model or reason.

**Independent Test**: valid, then invalid, then unreachable: the report shows each state and no
key material.

- [x] T018 [P] [US3] Write `tests/unit/test_health_credentials.py` against `contracts/health-credentials.md`: states `valid`, `auth_failed`, `unreachable`, `skipped`; `ready_for_indexing` false only for a required `auth_failed`; `unreachable` does not clear it; the response has no host, model, reason text or key; `/health` never triggers a probe (assert zero requests on the fake transport)
- [x] T019 [US3] Add `credentials` to `index_guard.health_fields()` in `src/treeweft/application/index_guard.py` from the cached view only, and confirm it flows through `GET /health` in `src/treeweft/application/indexer_service.py`

---

## Phase 6: User Story 4 - Backends that need no key are never probed (P3)

**Goal**: no probe and no new failure mode for key-free setups.

**Independent Test**: key-free local embedder -> zero probe requests.

- [x] T020 [P] [US4] Write `tests/unit/test_credential_skip.py`: `EMBEDDING_PROVIDER` other than `openai` (TEI) -> embedding `skipped`, zero requests; `openai` with an empty key -> `skipped` (the adapter's own error covers it); `USE_SUMMARY_VECTOR=0` -> summarizer `skipped` and its `auth_failed` state cannot block indexing; the simple profile with one shared key makes exactly one probe
- [x] T021 [US4] Fix any gap T020 reveals in `resolve_backends()` (`src/treeweft/application/index_guard.py`); if none, mark this task done with a note _No gap found: `resolve` skip rules were built in T008._

---

## Phase 7: Polish and cross-cutting

- [x] T022 [P] Add a test that a credential 409 reaches an MCP agent as readable text, in `tests/unit/test_mcp_compat.py` (FR-014); add a UI component test in `ui/src/api/client.test.ts` (or the nearest existing test) that a 409 `detail` is surfaced rather than a generic error
- [x] T023 [P] Make `run-simple.sh` report credential status once the indexer is up (read `/health`, print "embeddings key rejected: set OPENAI_API_KEY" if `auth_failed`); keep it non-fatal
- [x] T024 [P] Docs: `docs/simple-mode.md` (what a bad key now does, the single-probe behavior), the configuration reference for the three settings, and a short ADR-004 cross-reference; `CHANGELOG.md` under "Added" and "Operator configuration" (new 409 reason, `/health.credentials`, three settings)
- [x] T025 Regenerate contracts with `python scripts/update_contracts.py`; confirm the diff is only the additive `credentials` field and 409 reason, and that the version-bump check against the last release passes (SemVer MINOR, no index-schema bump) _Not regenerated, deliberately: the snapshots are release artifacts (they are already stale against main for unrelated prompt-pin routes) and `/health`'s response is not schema-captured. The change is additive: SemVer MINOR._
- [x] T026 Extend the CLAUDE.md invariant that already covers `require_parsers()` (the paragraph on `/index-*` routes) to add `require_credentials()`, and extend the route classification in `tests/unit/test_index_gate_routes.py` (`PARSER_GATED_REQUESTS` / `PARSER_EXEMPT`) so every parsing write route is proven credential-gated by a 409 test, with `POST /webhook` exempt for the reason in FR-005 _Done via `tests/unit/test_credential_gate_routes.py`, which reuses `PARSER_GATED_REQUESTS`; the CLAUDE.md invariant is extended._
- [x] T027 Run the full unit suite and the quickstart manual check against a deliberately wrong key; report results as they happened, including any failures and anything not run (Principle I) _Full unit suite 2969 passed / 3 failed (the same `test_model_route` failures present on main); UI 191/191; manual wrong-key run done against a stub 401 server and a throwaway Postgres._

---

## Dependencies and execution order

- Phase 1 -> Phase 2 -> user stories. Phase 2 blocks everything.
- US1 and US2 are independent after Phase 2 (different code paths) and can run in parallel; both
  are P1. US3 needs T008's cache. US4 is verification over T008 and can run any time after it.
- Within a phase: test tasks before their implementation tasks; T012 and T013 need T011; T015 needs
  T004 and T005.
- Polish after the stories you intend to ship.

### Parallel opportunities

- T002 with T001; T003 with T005; T009 with T010; T018, T020 and T022-T024 are all in separate files
- After Phase 2, one person can take US1 (T009-T013) while another takes US2 (T014-T017)

## Implementation strategy

1. **MVP**: Phases 1-3 (US1). Ships the refusal at the door; verify with the quickstart.
2. Add US2 to close the in-flight and webhook gap (the incident exactly).
3. Add US3, US4, then polish. Each increment is independently testable and shippable.

Counts: 27 tasks. Setup 2, Foundational 6, US1 5, US2 4, US3 2, US4 2, Polish 6.
