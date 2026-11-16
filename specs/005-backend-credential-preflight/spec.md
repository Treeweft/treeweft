# Feature Specification: Backend Credential Pre-flight and Fail-Fast Indexing

**Feature Branch**: `005-backend-credential-preflight`

**Created**: 2026-11-15

**Status**: Draft

**Input**: User description: "Pre-flight credential validation for embedding and LLM backends. Before
an index job is queued, and again early in a running job, probe the configured embedding endpoint
(and the summarizer LLM when configured). Only an authentication failure means an invalid key;
timeouts and server errors are 'unreachable', not invalid. Provider-neutral. When the key is
invalid, index routes answer 409 naming the backend (never the key), the push webhook still queues
its job, and a running job that keeps seeing auth failures stops fast instead of grinding through
every file. Cache a pass briefly, expose status in the health report, skip the check for local
key-free embedders."

## Background

An operator configured a wrong embeddings API key. An index job for a 37,019-file repository was
accepted, started, and then made one failing call per file: every embeddings request returned
`401 Incorrect API key provided`. After roughly ten minutes the job was at 10 of 37,019 files with
an estimated completion time measured in weeks, still reporting `running`. Nothing told the operator
the credential was the cause; the only evidence was a repeating error in the indexer log.

This is the failure Constitution Principle V ("Fail Loud, Never Degrade Silently") exists to
prevent: a component that keeps "working" while producing nothing. The same principle already
gates index routes on parser readiness; this feature applies the same treatment to the credentials
of the model backends an index job depends on.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A bad key is refused before work is queued (Priority: P1)

An operator (or an agent acting for one) asks the indexer to index a repository while the
embedding backend's credential is wrong. The request is refused immediately with a message that
says which backend failed authentication and what to do, instead of queuing a job that is
guaranteed to produce nothing.

**Why this priority**: This is the whole defect. Refusing at the door turns a silent
multi-hour-or-never failure into an instant, actionable one, and it needs no change to running-job
behavior to deliver value.

**Independent Test**: Configure an embedding backend that answers 401, submit an index request on
each gated route, and confirm each is refused with a conflict response naming the backend and the
reason, no job is created, and the credential value appears in neither the response nor the log.

**Acceptance Scenarios**:

1. **Given** an embedding backend that rejects the configured credential, **When** a client submits
   an index request to any gated route, **Then** the request is refused with a conflict response
   whose reason identifies an embedding authentication failure and names the backend (its URL
   host and configured model, never the credential), and no job record is created.
2. **Given** a valid credential, **When** a client submits an index request, **Then** the request
   is accepted exactly as today.
3. **Given** a backend that times out or answers with a server error, **When** a client submits an
   index request, **Then** the request is not refused for a credential reason (the backend is
   merely unreachable; existing behavior for that case is unchanged).
4. **Given** a backend that rejected the credential and the operator then fixes it, **When** the
   next check window elapses, **Then** index requests are accepted again without restarting the
   indexer.

---

### User Story 2 - A running job stops fast when credentials fail (Priority: P1)

A job is already running (it was queued before the key was revoked or rotated, or came in through
the push webhook, which cannot be refused). It starts seeing authentication failures. Instead of
failing file after file at the same rate as a healthy job, it detects that the failures are
credential failures, stops after a small bounded number of consecutive ones, and ends in a clear
terminal state that names the cause.

**Why this priority**: Without this, the incident in the Background recurs for any job that was
accepted before the credential went bad, and for every webhook-queued job.

**Independent Test**: Start a job against a backend that begins answering 401 after N successful
calls; confirm the job ends within a bounded number of further calls in a failed state whose reason
names the embedding authentication failure, rather than continuing through the remaining files.

**Acceptance Scenarios**:

1. **Given** a running job, **When** consecutive embedding calls fail authentication past a
   threshold, **Then** the job ends `failed` with a reason naming the backend and the cause, and
   makes no further embedding calls.
2. **Given** a running job that sees one transient authentication-shaped failure followed by
   successes, **When** the run completes, **Then** the job is not failed (only consecutive
   failures count).
3. **Given** a job that ended because of a credential failure, **When** the operator has fixed the
   key and retries the job, **Then** it can be retried through the existing retry path.
4. **Given** the push webhook receives a push while the credential is invalid, **When** it
   handles the push, **Then** it still queues the job (a refusal would drop the push) and the job
   ends as in scenario 1 and is recoverable by retry.

---

### User Story 3 - Operators can see credential health without starting a job (Priority: P2)

An operator opens the health report (or the operator UI that reads it) and sees whether each
backend role's credential was last seen valid, invalid, or unreachable, and when it was last
checked.

**Why this priority**: It lets the operator find and fix the problem before anyone submits work,
but the feature already prevents the damage without it.

**Independent Test**: With a valid credential, then an invalid one, read the health report and
confirm each state and reason appears and that the credential value never does.

**Acceptance Scenarios**:

1. **Given** a backend whose credential was last seen valid, **When** the health report is read,
   **Then** it shows that backend as valid with a check time.
2. **Given** a backend that rejected the credential, **When** the health report is read, **Then**
   it shows that backend role as `auth_failed` (no host, model or free-text reason), and the
   overall report marks indexing as not ready.
3. **Given** a backend that is unreachable, **When** the health report is read, **Then** it shows
   `unreachable`, distinct from `auth_failed`.

---

### User Story 4 - Backends that need no key are never probed (Priority: P3)

An operator runs a local embedding service that takes no credential (for example a local
inference server on the same host). Index requests proceed with no credential probe, no added
latency and no new failure modes.

**Why this priority**: It protects the default local and simple-mode experience from regression;
it is a constraint more than a capability.

**Independent Test**: Configure a key-free local embedding endpoint and confirm index requests
make no extra probe request and behave exactly as before.

**Acceptance Scenarios**:

1. **Given** an embedding backend configured without a credential, **When** an index request is
   submitted, **Then** no credential probe is made and the request is accepted as today.

### Edge Cases

- Fail versus pause: a job that stops on credential failures ends `failed` and is retried through the existing retry path (the parser-readiness precedent), rather than staying paused; see Assumptions.
- A pass is cached briefly; a key revoked inside that window is caught by the running-job
  fail-fast (User Story 2), not by the pre-flight.
- The probe itself fails for a non-authentication reason (timeout, DNS, TLS, 5xx, rate limit 429):
  never reported as an invalid credential, and never blocks indexing on its own.
- A backend answers 403: treated as an authentication failure only when it is an authorization
  response to the credential, not for a missing model or exhausted quota; those are reported with
  their own reason when the backend says so.
- Several embedding backends are configured (primary and fallback, or a registry): each is checked;
  indexing is refused only when no usable backend remains, and the reason lists the failing ones.
- The credential contains characters that would appear in an error body echoed by the provider
  (the incident's 401 body echoed a key prefix): any provider text surfaced to the operator is
  scrubbed so no part of the credential is returned or logged.
- The probe is itself a billable request on some providers: it is a single minimal input and is
  cached so a burst of index requests produces one probe.
- Two requests arrive at the same moment with no cached result: one probe is made and shared.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Before an index job is queued, the system MUST verify the credential of each
  embedding backend the job will use, by one minimal request to that backend.
- **FR-002**: The system MUST classify the outcome as exactly one of `valid`, `auth_failed`
  (the backend rejected the credential), or `unreachable` (any other failure). Only
  `auth_failed` blocks indexing; `unreachable` MUST NOT be reported as an invalid credential.
- **FR-003**: The check MUST work against any embeddings backend that speaks the supported wire
  protocol and MUST NOT assume a particular vendor.
- **FR-004**: When the outcome is `auth_failed`, each gated index route (file, directory,
  repository, graph, job retry, index rebuild) MUST refuse with a 409 conflict response carrying
  a stable machine-readable reason, the backend identity (host and model) and a pointer to where
  the credential is configured by setting name. The response MUST NOT contain the credential or
  any prefix of it.
- **FR-005**: The push webhook MUST NOT be refused on credential grounds; it MUST queue its job as
  today, and that job MUST end as in FR-007.
- **FR-006**: When per-chunk summaries are enabled for indexing (not merely when a language-model
  endpoint is configured) and the summarizer backend's credential is `auth_failed`, the system
  MUST refuse indexing exactly as for an embedding failure, with a reason that names the
  summarizer backend. When summaries are disabled, the summarizer backend MUST NOT be probed and
  its credential state MUST NOT block indexing. A language-model credential used only at search
  time (query expansion) is outside this gate; its state appears in the health report (FR-010).
- **FR-006a**: When two checked backends resolve to the same endpoint host and credential, the
  system MUST make one probe and apply its outcome to both, naming each affected backend in the
  reason.
- **FR-007**: A running job MUST count consecutive embedding authentication failures; once the
  count reaches a bounded threshold it MUST stop making embedding calls and end in a terminal
  failed state whose reason names the backend and cause. The threshold MUST be configurable with a
  documented default, and a single success MUST reset the count.
- **FR-008**: A job ended under FR-007 MUST be recoverable through the existing retry path once the
  credential is fixed, and MUST NOT leave partially indexed content indistinguishable from a
  complete index.
- **FR-009**: A passing result MUST be cached for a configurable interval (documented default);
  an `auth_failed` result MUST be re-checked after a shorter configurable interval so a corrected
  key is accepted without a restart. Concurrent callers with no cached result MUST share one probe.
- **FR-010**: The health report is public, so it MUST expose, per backend role, only the last
  outcome (`valid`, `auth_failed`, `unreachable`, or `skipped` when the backend is not required)
  and the check time, and MUST mark indexing not ready while a required backend is `auth_failed`.
  It MUST NOT include the backend host, model, free-text reason or any credential material; those
  appear only in the refusal an authenticated caller receives (FR-004) and in the failed job's
  error.
- **FR-011**: The system MUST NOT probe an embedding backend that is configured without a
  credential.
- **FR-012**: The credential, and any prefix, suffix or hash of it, MUST NOT appear in responses,
  health output, job error text or logs; provider error text that echoes it MUST be scrubbed
  before being stored or logged.
- **FR-013**: Every refusal and every fail-fast stop MUST be logged once per state change at a
  level that makes it observable (Constitution Principle V), and covered by a test that shows
  when it engages.
- **FR-014**: The operator and agent-facing tool surface (the MCP tools and the UI) MUST show the
  reason text for a credential refusal rather than a generic error.

### Key Entities

- **Backend Credential Status**: the last known outcome (`valid`, `auth_failed`, `unreachable`)
  for one backend, with the backend identity (host and model), the time checked and a scrubbed
  reason. Held per process; derived, not authoritative.
- **Credential Failure Streak**: the count of consecutive authentication failures seen by one
  running job, reset by any success; drives the fail-fast stop.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With an invalid embeddings credential, 100% of index requests on the gated routes
  are refused in under 5 seconds, and none creates a job.
- **SC-002**: A job that begins seeing authentication failures ends failed after no more than the
  configured threshold of further failed calls (default no more than 5), never after thousands.
- **SC-003**: In a repository the size of the incident (37,019 files), an invalid credential costs
  the operator seconds to discover instead of an indefinitely running job.
- **SC-004**: With a valid credential or a key-free local backend, there is no measurable change
  in index request latency beyond one cached probe per check interval, and zero probes for
  key-free backends.
- **SC-005**: No test, log line, response or health output across the feature contains the
  credential or any part of it.
- **SC-006**: An operator can tell from the health report alone, without reading logs, which
  backend role's credential is failing (embedding or summarizer).

## Assumptions

- The embeddings backends in scope speak the OpenAI-compatible HTTP protocol; a 401 (and a 403 that
  denotes a rejected credential) is the signal for "invalid key". Other protocols are out of
  scope.
- "Key-free" means no credential is configured for that backend (no key value, or a local
  in-process embedder). The configuration already distinguishes these.
- The existing readiness gate, 409 response shape and job retry path are reused rather than
  redesigned; this feature adds a new refusal reason alongside `parsers_unavailable` and the index
  stamp refusals.
- Per Principle V the design favors visible refusal over silent fallback; no mode silently skips
  embedding when the key is bad.
- In the simple profile one credential normally serves both embeddings and the language model, and
  summaries are off by default, so the effective gate there is one embeddings probe.
- Rotating a key at runtime without restart is already possible only by restart; this feature does
  not add hot reload. A corrected key is picked up when the process is restarted or its
  configuration reloaded by existing means, and the `auth_failed` re-check interval covers
  backends whose key changes externally (for example a proxy that fixes its own upstream key).
- The retry and recovery behavior for webhook-queued jobs matches the parser-readiness precedent
  (queued, then fails with a reason, then recoverable).
