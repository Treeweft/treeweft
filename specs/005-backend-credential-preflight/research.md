# Research: Backend Credential Pre-flight and Fail-Fast Indexing

Each item: Decision, Rationale, Alternatives. Findings come from reading the code at `origin/main`
(`ff61a7f`), not from assumption.

## R1. Why the incident ran so long

**Finding**: `embedding_adapter._embed_batch` already does the right thing locally: a 4xx other
than 429 is logged (`embeddings API 401: ...`, first 200 chars of the body) and re-raised with no
retry. The damage is one level up. `_walk_and_index._process_one` wraps `_process_file` in
`except Exception`, increments `job["errors"]`, records a `_job_file_error_store` row and carries
on. `_terminal_status_for_index` only fails the job at the end, when `errors >= attempted_units`
and no chunks were produced. So every file fails fast, the job stays `running`, and the ETA climbs.

**Decision**: fix at both layers: refuse at the door (pre-flight) and stop at the first sign of a
credential streak (loop). The adapter's no-retry behavior is kept.

## R2. Where the gate goes

**Decision**: add `index_guard.require_credentials()` returning `JSONResponse | None`, called
immediately after `require_parsers()` on the same six call sites in `indexer_service.py`
(lines ~760, 800, 854, 931, 1399, 1613), and add a dispatch-time check next to
`_ParserGatedRunner` in `indexer_runners.py`.

**Rationale**: it is the existing, tested pattern: routes get a 409 with a stable reason; queued
jobs (including webhook-queued) fail at start and take the queue worker's exception path, so a
retryable kind is re-enqueued and finally dead-lettered rather than lost. `routes_webhook.py`
already documents why it must not gate (a 409 drops the push).

**Alternatives**: gate only in routes (misses queued/webhook jobs); gate only in the runner
(operator learns after queuing, and every refused call creates a job record).

## R3. How to probe

**Decision**: for embeddings, `POST {base}/embeddings` with a one-token input and the configured
model; for the summarizer, `POST {llm_url}/chat/completions` with `max_tokens=1` and a one-word
user message. Classification: 2xx -> `valid`; 401 -> `auth_failed`; 403 -> `auth_failed` only
when the body does not say model/quota/permission-for-model (otherwise `unreachable` with that
reason); everything else (timeout, DNS, TLS, 429, 5xx, other 4xx) -> `unreachable`.

**Rationale**: the real request shape is the only test that exercises key, base URL and model
together, and it is the one call the adapter will make anyway. `GET /models` was rejected: many
compatible servers lack it or do not authenticate it, so it can pass for a bad key. Cost is a
single minimal input per window.

**Alternatives**: `GET /models` (unreliable); reusing a real `embed()` of the first chunk (couples
the gate to chunking and burns a real batch).

## R4. Cache, single-flight, de-duplication

**Decision**: a process-local dict keyed by `(endpoint host+path, sha256(key))` held in memory only
(the hash is a cache key, never logged, returned or persisted; FR-012). A `valid` result lives
`TREEWEFT_CREDENTIAL_CHECK_SECONDS` (default 300); `auth_failed` and `unreachable` live
`TREEWEFT_CREDENTIAL_RETRY_SECONDS` (default 30). An `asyncio.Lock` per key gives single-flight.
When two roles resolve to the same key (simple profile: embeddings and LLM both use
`OPENAI_API_KEY`), they share an entry and one probe (FR-006a).

**Rationale**: mirrors `parsers._cached` (pass cached long, failure cached short) so an outage or a
fixed key is noticed without restart and without a probe per request.

**Alternatives**: persisting results (stale across restarts; no benefit); a background refresh
task (more moving parts than the on-demand pattern needs).

## R5. When the probe runs and what it skips

**Decision**: skip embeddings when `EMBEDDING_PROVIDER` is not `openai` (the TEI path has no
credential) or the resolved key is empty. Probe the summarizer only when `USE_SUMMARY_VECTOR` is
on. Note `config.py` defaults `USE_SUMMARY_VECTOR` to `1`; the simple profile sets it to `0`, which
is why the simple profile normally probes only embeddings. HyDE and any search-time LLM use are
excluded from the gate (FR-006) but reported in `/health`.

**Alternatives**: always probe the LLM (blocks simple-mode users whose LLM key is irrelevant to
indexing); make it a setting (adds a knob for a rare need).

## R6. Typed adapter error and the streak

**Decision**: add `BackendAuthError(RuntimeError)` carrying `role`, `host`, `model`, `status`;
the adapter raises it on 401/403 with the provider body dropped (only status, host and model are
kept). `_process_one` catches it before the generic handler, increments a per-job consecutive
counter, resets it when a file embeds successfully, and when it reaches
`TREEWEFT_CREDENTIAL_FAIL_STREAK` (default 5) sets a job-level abort flag. Remaining
`_process_one` coroutines check the flag first and return without calling the embedder. After the
gather, the runner raises so the job ends `failed` with `embedding_auth_failed: <role> <host>`.

**Rationale**: concurrency is bounded by a semaphore and asyncio is single-threaded, so a plain
counter is race-free between awaits (the code already relies on this for `total_chunks_ref`). The
files skipped by the abort are not counted as errors, so a retry resumes them (the walk already
supports resume). The partial content already written stays attributable: the job is `failed`, not
`done`, satisfying FR-008.

**Alternatives**: failing on the first 401 (one transient gateway blip would kill a long job);
counting all errors (a repo with many unreadable files would trip it).

## R7. What `/health` exposes

**Decision**: `/health` is public (`auth_middleware.PUBLIC_PATHS`), so it carries only role,
state and check time: `{"role": "embedding", "state": "auth_failed", "checked_at": ...}`. Host,
model and reason text appear in the 409 body (authenticated routes) and the job error. The report
sets `ready_for_indexing` false while a required role is `auth_failed`.

**Rationale**: Principle VI; backend hostnames and model names are deployment detail and the 401
echo risk is exactly why scrubbing matters. SC-006 is still met because the role names which
credential fails.

**Spec note**: this narrows FR-010 ("the refusal reason" in the health report) to a stable reason
*code*, not free text. If you want host and model in `/health`, make `/health` admin-aware first;
tracked as an open question below.

## R8. Credential scrubbing

**Decision**: a `scrub(text, secrets)` helper replaces any occurrence of a configured key, and of
any `sk-`-shaped token prefix, before text reaches a log, job error or response; the adapter stops
logging `exc.response.text[:200]` for 401/403 (OpenAI's body echoes a key prefix, as seen in the
incident log). Unit tests assert the key and every prefix of it are absent from responses, logs and
job errors.

## R9. Contracts and versioning

**Decision**: new fields and a new 409 reason are additive: SemVer MINOR. Regenerate
`contracts/api.json` with `scripts/update_contracts.py`; the version-bump check against the last
release must pass. No `INDEX_SCHEMA_VERSION` bump, no migration.

## R10. MCP and UI surfaces (FR-014)

**Decision**: the stdio MCP server already relays an indexer error's text to the agent
(`mcp_compat`); verify with a test that a credential 409 reaches the agent as text. The UI's
`ApiError` already prefers `detail` from the response body (`ui/src/api/client.ts`), so the 409
`detail` is shown with no UI change; add one component test to lock it.

## Open questions (not blocking)

- Should an admin-authenticated `/health` detail view show host/model/reason? Out of scope here.
- A gateway that blocks the probe request itself but allows real traffic would be reported
  `unreachable` (non-blocking), which is the safe direction.
