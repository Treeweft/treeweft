# Feature Specification: Prompt Versioning with Admin Pins and Summary-Only Refresh

**Feature Branch**: `002-prompt-versioning`

**Created**: 2026-09-25

**Status**: Draft

**Input**: User description: "lets work on ADR-003" — implement ADR-003 in full: the immutable
prompt registry (§1), pin storage and per-source tracking (§2), the `resummarize` job (§3), pin
seeding, the admin API with dry run, the operator UI and the docs (§4), and its tests (§5).

**Governing record**: [ADR-003](../../docs/adr-003-prompt-versioning.md). Where this spec and the
ADR disagree, the ADR wins until it is amended (constitution, "Development Workflow"). This spec
corrects two ADR gaps (FR-010, incremental jobs; FR-015, partly applied refreshes), and the ADR
was amended to match.

## Background

The chunk-summary prompt shapes every stored summary and its `summary_vector`. Today its version
is a code constant (`PROMPT_VERSION = 3`), so shipping a better prompt forces every deployment into
a full re-index on upgrade, whether its admin wants the new prompt or not. Nothing records which
prompt built a source's summaries, so a deployment cannot tell that they are stale. As a result,
prompt improvements have been avoided.

This feature turns prompts into shipped, immutable, versioned entries. Each deployment's admin
chooses (pins) the version it uses, per operation, with a per-source override for the chunk
summary. A release that adds a version changes nothing until an admin moves a pin. Moving the
chunk-summary pin refreshes only the summaries and their vectors, not the whole index.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An upgrade that ships a new prompt changes nothing until the admin opts in (Priority: P1)

An operator upgrades an existing deployment to a release that registers a new chunk-summary
prompt version. The indexer starts and keeps using the version its sources were built with. No
summary is regenerated, no source is marked stale and no LLM cost is spent. A fresh install, by
contrast, starts on the latest version. Search results and stored summaries are unchanged by the
upgrade itself.

**Why this priority**: This is the defect the ADR exists to remove. A release must never force LLM
work or a re-index on a deployment. It also carries the foundation every other story needs: the
registry, resolution of the effective version, pin storage and version tracking per source.

**Independent Test**: With a registry containing chunk-summary v3 and a test-only v4, start the
indexer (a) against existing sources built at v3 and no pins, and confirm the deployment pin is
seeded to v3, no source is stale and summaries are still written and read at v3; (b) against an
empty deployment, and confirm the pin is seeded to v4.

**Acceptance Scenarios**:

1. **Given** an existing deployment upgraded from a release without prompt versioning, **When**
   the indexer first starts, **Then** every existing source is recorded as built with chunk-summary
   v3, the deployment pins are seeded to chunk-summary v3 and HyDE v1, and no source is stale.
2. **Given** a fresh install with no sources, **When** the indexer first starts, **Then** the
   deployment pins are seeded to the latest registered version of each operation.
3. **Given** seeded pins, **When** a later release registers a newer version, **Then** the pins do
   not move, no source becomes stale, and no refresh is enqueued.
4. **Given** a pin, **When** a chunk is summarized during indexing, **Then** the summary is
   produced by the pinned version's prompt, validated against that version's output schema, and
   cached under that version.
5. **Given** a HyDE pin, **When** a query is expanded, **Then** the pinned HyDE version's prompt
   is used and its expansion is cached under that version.
6. **Given** a stored pin naming a version this build does not know (after a downgrade), **When**
   the indexer starts, **Then** startup fails with a message naming the pin and the available
   versions.
7. **Given** any registered prompt version, **When** a developer edits its text or output schema
   instead of adding a new version, **Then** the unit suite fails and names the edited version.

---

### User Story 2 - An admin moves the chunk-summary pin and only the summaries are refreshed (Priority: P1)

An admin decides to adopt a newer chunk-summary version. They first preview the change with a dry
run, which lists every source that would be refreshed, its current and target versions, its chunk
count and the total, and changes nothing. They then apply it. Every source now out of date is
marked stale and gets a background summary-only refresh. The refresh regenerates each chunk's
summary at the new version (reusing any already cached), re-embeds only the summaries and writes
them back. It does not parse files, re-embed code or rebuild the graph. When a source's refresh
finishes cleanly it becomes current. Search keeps working throughout.

**Why this priority**: This delivers the cost saving. A prompt change costs LLM calls for uncached
chunks plus summary embeddings, instead of a full re-index. It depends on Story 1's foundation.

**Independent Test**: Against a fake vector store, index a source at v3, move the deployment pin
to v4 with a dry run then for real, run the refresh, and confirm: the dry run matches what the real
call enqueues and writes nothing; after the refresh every chunk carries a new summary vector, row
count and code vectors are unchanged, no parsing or code embedding ran, and the source is current
at v4.

**Acceptance Scenarios**:

1. **Given** sources at v3 and a pin at v3, **When** an admin requests a dry run of moving the
   deployment chunk-summary pin to v4, **Then** the response lists each source that would be
   refreshed with its current version, target version and chunk count, plus the total chunk count,
   and no pin is written, no other process is notified and no job is enqueued.
2. **Given** the same state, **When** the admin moves the pin to v4 for real, **Then** the pin is
   stored with who changed it and when, every stale source without an override gets exactly one
   refresh job, and the response lists those jobs.
3. **Given** a refresh job, **When** it runs, **Then** every chunk of the source gets a summary
   and a summary vector at the target version (cache hits reused, rejection markers respected), a
   chunk with no summary gets the store's own no-summary value as at index time (a zero vector
   on Milvus, NULL on LanceDB), and code vectors,
   full-text search data, metadata and the chunk count are unchanged.
4. **Given** a refresh in which every chunk got a summary or a rejection marker, **When** it
   completes, **Then** the source's recorded version becomes the target and it is no longer stale.
5. **Given** a refresh in which some chunks failed transiently (LLM timeout or error), **When** it
   completes, **Then** the job ends as done with errors, the recorded version does not change, the
   source stays stale, and re-running it costs no new LLM calls for chunks that were summarized.
6. **Given** a source recorded at v3 whose refresh toward v4 ended with errors, **When** the admin
   moves the pin back to v3, **Then** the source is still reported stale and a refresh to v3 is
   enqueued. After that refresh finishes cleanly, the source is current at v3.
7. **Given** a refresh in progress, **When** anyone searches the source, **Then** search works and
   returns its chunks. Their summary vectors may come from either version until the refresh
   completes.
8. **Given** a request to set a pin to an unregistered version, as a dry run or for real, **When**
   it is sent, **Then** it is refused with a client error listing the valid versions.
9. **Given** a HyDE pin change, dry run or real, **When** it is sent, **Then** no job is
   enqueued. The response says the change takes effect on the next query, and after a real change
   no expansion from the previous version is served, in any indexer process.

---

### User Story 3 - An admin trials a version on one source before promoting it (Priority: P2)

An admin sets a chunk-summary override on one source, which refreshes only that source. They
compare its search behaviour with the rest, then either promote the version to the deployment pin
or remove the override, which returns the source to the deployment pin and refreshes it back if
needed. The admin can also request a refresh of a single source by hand.

**Why this priority**: Trialling a prompt on one source before paying for the whole deployment is
the safe rollout path the runbook describes. The deployment pin in Story 2 is enough to adopt a
version, so this refines it.

**Independent Test**: With two sources at v3 and the deployment pin at v3, set an override to v4
on one source. Confirm only that source is refreshed and the other stays current. Then move the
deployment pin to v4 and confirm the overridden source is not re-enqueued. Remove the override and
confirm the source follows the deployment pin and is not refreshed when already at it.

**Acceptance Scenarios**:

1. **Given** a source at v3 and a deployment pin at v3, **When** an admin sets an override of v4
   on it (dry run first, then for real), **Then** only that source becomes stale and gets a
   refresh job.
2. **Given** a source with an override, **When** the deployment pin moves, **Then** that source is
   not enqueued, and its target stays its override.
3. **Given** a source with an override at its own recorded version, **When** the override is
   removed, **Then** its target becomes the deployment pin, and it is enqueued only if that differs
   from its recorded version.
4. **Given** a request to set a per-source override for HyDE, **When** it is sent, **Then** it is
   refused with a client error. HyDE pins are deployment-wide.
5. **Given** a source, **When** an admin requests a manual refresh, **Then** a refresh job is
   enqueued if the source is stale. If it is already current, the request succeeds and reports
   that there was nothing to do, subject to FR-015.
6. **Given** a source with an override, **When** the source is deleted, **Then** its override is
   deleted with it.

---

### User Story 4 - Operators see and manage prompt versions in the UI (Priority: P3)

An admin opens a **Prompts** page next to Backends. For each operation they see the registered
versions with their release notes and the current deployment pin. A sources table shows each
source's built version, target version, a stale badge, an override control and a refresh action.
Every pin change first shows the dry-run result (sources affected, total chunks) in a confirmation
step, and only confirming applies it.

**Why this priority**: The API in Stories 2 and 3 is complete on its own. The page makes staleness
visible without API calls and puts the dry run in front of every change.

**Independent Test**: With the API mocked, render the page and confirm it shows versions, notes,
pins and per-source staleness, that changing a pin calls the dry run and shows its result before
any real request, and that cancelling sends nothing.

**Acceptance Scenarios**:

1. **Given** an admin, **When** they open the Prompts page, **Then** they see every operation's
   versions, notes, latest version and deployment pin, and every source's built version, target
   version and stale status.
2. **Given** the page, **When** the admin chooses a new pin or override, **Then** the dry-run
   result is shown in a confirmation step, and cancelling sends no real request.
3. **Given** a confirmation, **When** the admin confirms, **Then** the real request is sent, and
   the page shows the jobs it enqueued and the updated staleness.
4. **Given** a non-admin user, **When** they open the page, **Then** it shows that admin access is
   required (as the Backends page does), and the underlying requests are refused.

### Edge Cases

- **A pin moves again while a refresh is running or partly done.** A source's vectors can be
  mixed after a refresh that failed partway or was overtaken by another pin change. Its recorded
  version has not advanced. If the pin then moves back to that recorded version, the source looks
  current while holding summary vectors from the other version. To prevent this, a refresh marks
  the source as in progress toward its target when it starts, and only a clean finish clears the
  mark (FR-015). While the mark is set, the source is stale whatever its target, so reverting the
  pin re-enqueues it rather than hiding the mixed vectors.
- **Incremental and full index jobs during a migration.** Incremental jobs (webhooks) insert their
  changed files' chunks with no summary vectors at all and never change the recorded version; a
  later refresh or a full index job fills them in. A full index job re-summarizes every chunk at
  the target and does advance the recorded version. Only a full index job or a clean refresh may
  advance the recorded version (FR-010).
- **The index is not writable** (ADR-004 `reindex_required`, `unverified`, or stores being
  recreated during a rebuild). Refresh jobs write vectors, so they are refused like any other index
  job. A pin change is still stored. The response says which refreshes could not be enqueued and
  why. The affected sources stay stale until a later refresh. After an ADR-004 rebuild, every
  source is fully re-indexed at its target version, so it becomes current without a refresh.
- **A source already has an active job** (index, incremental or refresh). The one-active-job rule
  forbids a second job, so the refresh is **deferred**: the response lists the source and the
  blocking job, and the refresh is enqueued when that job finishes, if the source is still stale.
  A refresh is never enqueued twice for one source, and a finished refresh re-triggers only when
  a pin change overtook it.
- **Index work arrives while a refresh is active** (a webhook push, an index request, a graph
  rebuild, a fleet refresh). Index work preempts the refresh, so no request is dropped. A queued
  refresh is cancelled and the index job enqueued. A running refresh gets a `waiting` index job
  behind it; the refresh stops at its next batch, leaving the source marked stale, and the waiting
  job then runs. Several waiting jobs run one after another in arrival order. The refresh is
  re-enqueued afterwards if the source is still stale. (Code-review decision, 2026-09-26.)
- **A source is deleted while its refresh is queued or running.** The refresh ends without writing
  and without error beyond noting the source is gone.
- **A source with summary vectors disabled, or a vector store without summary vectors**
  (ChromaDB). In simple mode `USE_SUMMARY_VECTOR` defaults to off. A refresh is then a no-op. It reports that summary vectors are disabled and leaves the recorded
  version unchanged. In the full stack, a source indexed with summary vectors off was recorded as
  v3 by the migration; a refresh populates its summary vectors.
- **A source never summarized** (recorded version unknown). It is never reported stale and is
  never refreshed automatically. A full index job records its version.
- **Seeding when sources exist but none has a recorded version.** The deployment chunk-summary pin
  is seeded to the latest version, as for a fresh install, because nothing was built with any
  version.
- **Several indexer processes.** A pin change in one process reaches every process: summaries
  and HyDE expansions use the new version, and stale HyDE expansions are dropped, everywhere.
- **Milvus row identity.** On the Milvus version in use, rewriting a row changes its primary key.
  The refresh works from a snapshot of the source's row IDs taken before it writes, never from
  live iteration, and reads with strong consistency. No row is refreshed twice or skipped.

## Requirements *(mandatory)*

### Functional Requirements

**Prompt registry**

- **FR-001**: The system MUST hold an append-only registry of prompt versions per operation
  (`chunk_summary`, `hyde`). Each version has its system prompt text, the output schema its
  response is validated against, and a one-line release note.
- **FR-002**: The registry MUST be seeded with chunk-summary v3 and HyDE v1, identical to today's
  prompts and schemas, so every existing cached summary stays valid.
- **FR-003**: A shipped version MUST be immutable. A unit test MUST compare a hash of every
  registered version's text and schema with a committed fixture. It MUST fail when a shipped
  version is edited, and when a version is added without its hash.
- **FR-004**: Every place that uses the summary or HyDE prompt, schema or version constant MUST
  resolve it from the registry through the effective version. The user-message builders (including
  the nonce-fenced data fence) and the no-think suffix handling MUST stay shared and unversioned,
  so every version keeps the same prompt-injection defences (constitution VI).

**Resolution and caching**

- **FR-005**: The effective HyDE version MUST be the deployment pin. The effective chunk-summary
  version for a source MUST be its override if one is set, otherwise the deployment pin.
- **FR-006**: Resolution MUST come from an in-memory view of the pins, loaded at startup and
  refreshed when any process changes a pin. It MUST NOT query the database per chunk or per query.
  Every indexer process MUST see a pin change within 5 seconds (SC-008).
- **FR-007**: Summary-cache entries MUST be written and read under the version resolved for the
  call, so several versions' entries coexist. The cache's key and the existing per-request
  summary-version read override MUST keep working unchanged.
- **FR-008**: HyDE expansions MUST be cached per version, and the HyDE cache MUST be cleared in
  every process when the HyDE pin changes.

**Pin storage and per-source tracking**

- **FR-009**: The system MUST store one deployment pin per operation and at most one per-source
  override for the chunk summary, each with the version, when it changed and who changed it. It
  MUST reject a HyDE override at the storage level as well as in the API.
- **FR-010**: Each source MUST record the chunk-summary version its stored summary vectors were
  built with. Existing sources MUST be recorded as v3 when the migration runs. The recorded version
  MUST be set when a **full** index job of the source completes cleanly (no failed files and no
  transient summary failures), to the version that job used, and when a refresh
  completes cleanly (FR-014). A clean full index job with summary vectors off, or on a vector
  store without them, MUST record the version as unknown, because the source then holds no
  summary vectors. An **incremental** index job MUST NOT change it, because it
  inserts changed files' chunks with no summary vectors at all. *(Amends ADR-003 §2, which also
  set it on incremental completion.)*
- **FR-011**: A source MUST be reported stale when its recorded version is known and differs from
  its effective chunk-summary version, and when FR-015 says so.
- **FR-012**: Deleting a source MUST delete its override.

**Seeding and startup**

- **FR-013**: At startup, for each operation without a deployment pin: with no sources, the latest
  registered version MUST be seeded. With sources, the chunk-summary pin MUST be seeded to the most
  common recorded version (the latest if none is recorded), and the HyDE pin to v1. A later release
  registering new versions MUST NOT move any pin. Startup MUST fail, naming the pin and the
  available versions, if a stored pin names a version this build does not register
  (constitution V).

**Refresh job**

- **FR-014**: The system MUST provide a summary-only refresh job for one source. It MUST:
  - snapshot the source's stored row IDs before writing, then process them in batches;
  - regenerate each chunk's summary at the source's target version through the existing summary
    path (cache hits and rejection markers reused);
  - embed the summaries through the existing embedding path;
  - write each chunk back with its new summary vector and every other stored field unchanged;
  - give a chunk whose summary was rejected the store's own no-summary value, as at index time
    (a zero vector on Milvus, NULL on LanceDB); a chunk whose summary failed transiently keeps
    its existing summary vector;
  - report progress as chunks processed out of total through the existing job fields;
  - advance the recorded version to the target only when every chunk got a summary or a
    rejection marker. Otherwise it ends as done with errors (failed, if every chunk failed) and
    leaves the version unchanged;
  - write nothing, and remove anything it just wrote, once the source has been deleted;
  - run no parsing, code embedding or graph work.
- **FR-015**: A refresh left partly applied, or overtaken by a pin change, MUST NOT let the source
  be reported current while its summary vectors are mixed. When a refresh starts, before it writes
  anything, it MUST record on the source the version it is refreshing toward. A clean finish MUST
  set the recorded version to that target and clear the mark in one step. A refresh that ends
  with errors, is interrupted or fails MUST leave the mark set. While the mark is set, the source
  MUST be reported stale for every target, including the recorded version, and every pin change
  that affects the source MUST enqueue a refresh for it. A full index job that completes cleanly
  also clears the mark. *(Amends ADR-003 §2 and §3.)*
- **FR-016**: Refresh jobs MUST be enqueued when the deployment chunk-summary pin changes (for
  every stale source without an override), when an override is set or removed (for that source,
  if stale), and on an admin's manual request. A source already at its target MUST get no job. A
  refresh MUST obey the one-active-job-per-source rule. A source with an active job MUST be
  reported as deferred and get its refresh when that job finishes, if still stale. An interrupted
  refresh MUST be resumed after an indexer restart. A refresh overtaken by a pin change while it
  ran MUST be followed by a refresh to the new target when it finishes. Decisions about a pin
  (a refresh's target, a pin change's no-op check, the post-job hook) MUST use the stored pins,
  not a process's cached view.
- **FR-017**: A refresh MUST be refused, like any other job that writes vectors, while the index is
  not writable (ADR-004 index guard). A pin change made then MUST still be stored, and its response
  MUST list the refreshes that could not be enqueued and why.
- **FR-018**: With summary vectors disabled, or on a vector store without summary vectors, a
  refresh MUST be a no-op that says so and leaves the
  recorded version unchanged.
- **FR-019**: Before relying on row rewrites that change primary keys, the implementation MUST
  confirm that nothing outside the vector store keeps vector-store row IDs (for example the search
  audit). If something does, that MUST be resolved first.

**Admin API**

- **FR-020**: Every endpoint in this section MUST be admin-only through the existing authz layer,
  and every mutation MUST record the caller (constitution VI).
- **FR-021**: The system MUST provide a read of the registry (versions, notes and latest per
  operation), the current pins (deployment and overrides), and, per source, its recorded version,
  effective target and stale flag.
- **FR-022**: The system MUST let an admin set the deployment pin of an operation, and set or clear
  a source's chunk-summary override. Each response MUST list the refresh jobs it enqueued.
- **FR-023**: Each of the three pin mutations MUST accept a dry run. It MUST validate exactly as
  the real call does and return the same response shape, marked as a dry run. In place of job IDs
  it MUST list the sources that would be refreshed, each with its current version, target version
  and chunk count, plus the total chunk count. It MUST write no pin, notify no process and enqueue
  no job. The chunk count is an upper bound on LLM calls; the dry run does not predict cache hits.
- **FR-024**: Setting a pin to an unregistered version MUST be refused with a client error listing
  the valid versions. A HyDE override MUST be refused with a client error.
- **FR-025**: The system MUST let an admin request a manual refresh of one source.
- **FR-026**: Pin changes and manual refreshes MUST be logged with the caller, the old and new
  version, and the sources affected.

**Operator UI**

- **FR-027**: The UI MUST provide an admin-only Prompts page next to Backends showing, per
  operation, the registered versions with notes and the deployment pin selector, and a sources
  table with built version, target version, stale badge, override control and refresh action.
- **FR-028**: Every pin or override change in the UI MUST first call the dry run and show its
  result in a confirmation step. Only confirming sends the real request.

**Docs, governance and release**

- **FR-029**: The engineering notes MUST replace "bump `PROMPT_VERSION` when prompts change" with
  "register a new version; never edit a shipped one", and describe the rollout flow. A new runbook
  MUST describe trialling a version with an override, comparing, promoting the deployment pin and
  watching the refresh jobs. `CLAUDE.md` and any other doc that says to bump `PROMPT_VERSION` MUST
  be updated to match.
- **FR-030**: ADR-003's status MUST record it as implemented, including the FR-010 and FR-015
  amendments.
- **FR-031**: The new endpoints are additive. The source version MUST satisfy the
  contract-snapshot check: 1.1.0 is unreleased and already a MINOR above the released 1.0.0, so it
  stays 1.1.0, or becomes 1.2.0 if 1.1.0 is released first. And the changelog MUST list the new endpoints and the
  migration. The feature MUST NOT bump `INDEX_SCHEMA_VERSION` or cause `reindex_required`
  (constitution VII). Its database changes MUST ship as a new numbered migration.

### Key Entities

- **Prompt version**: an immutable, shipped prompt for one operation: its version number, system
  text, output schema and release note.
- **Pin**: the admin's choice of version for an operation, either deployment-wide or, for the
  chunk summary only, a per-source override. It records when it changed and who changed it.
- **Source's recorded summary version**: the chunk-summary version the source's stored summary
  vectors were built with. It may be unknown (never summarized).
- **Effective version / target**: what a source (or the deployment, for HyDE) should use now: the
  override if any, otherwise the deployment pin.
- **Refresh-in-progress mark**: on a source, the version an unfinished refresh was moving it
  toward. While set, the source's summary vectors may be mixed.
- **Staleness**: a source whose recorded version is known and differs from its target, or whose
  refresh-in-progress mark is set.
- **Refresh (resummarize) job**: a background job that brings one source's summaries and summary
  vectors to its target version without re-indexing.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Upgrading an existing deployment to a release that registers a new chunk-summary
  version causes zero summary-generation calls, zero refresh jobs and zero stale sources, and
  search results for 3 fixed queries are identical before and after.
- **SC-002**: Refreshing a source to a new version makes zero file-parsing and zero code-embedding
  calls. Its chunk count and code vectors are identical before and after, and every chunk's
  summary vector changes (except chunks with no summary at either version).
- **SC-003**: Re-running a refresh that previously failed partway makes zero new
  summary-generation calls for chunks already summarized at the target version.
- **SC-004**: For every pin change in the test table, the dry run's list of sources and total
  chunk count are exactly what the real call then enqueues, and a dry run writes nothing, notifies
  nothing and enqueues nothing.
- **SC-005**: An admin can see which sources are stale, trial a version on one source, and promote
  it to the deployment, using only the Prompts page and the runbook, without reading code or
  touching a store directly.
- **SC-006**: Editing any shipped prompt version's text or schema fails the unit suite in 100% of
  cases. The unit suite stays free of external services.
- **SC-007**: In the Milvus integration test, after a refresh the source has the same row count,
  no duplicate chunks, the new summary vectors present, full-text and dynamic fields intact, and
  both dense and full-text search still return its chunks.
- **SC-008**: With two indexer processes, a pin change made through one is used by both within 5
  seconds, and neither serves a HyDE expansion from the previous version after that.

## Assumptions

- Only chunk-summary v3 and HyDE v1 are registered by this feature. It ships no new prompt, so it
  moves no retrieval default. Registering a new version later is a retrieval change, and making it
  a fresh install's default needs a benchmark run first (constitution III).
- Every prompt version's summaries are embedded with the same embedding model, so vectors from
  different versions stay comparable during a migration. Retrieval quality in that window is a
  blend of the two prompts, as the ADR accepts.
- Community summaries (a string template), the prompt enhancer (no LLM calls) and the benchmark
  prompts are not versioned (ADR "Out of scope").
- Admin-authored prompts, side-by-side A/B versions for one chunk, per-row version tags in the
  vector store and per-source HyDE pins are out of scope.
- The pin-change propagation reuses the notification mechanism of the embedding-backends
  registry. `/speckit-plan` settles the fallback if a notification is missed.
- The refresh reuses the existing embed batch size, embedding proxy and summary path. It has no
  per-row progress marker; a retry redoes the source, cheaply because of the summary cache.
- Without Postgres (`DATABASE_URL` unset) there are no pins: the baseline versions
  (chunk-summary v3, HyDE v1) are used, so an upgrade changes no prompt. Startup logs a warning
  saying so, and the pin endpoints report that they need Postgres.
- The next Postgres migration number is 021, as the ADR assumes.
- The source version is 1.1.0 and unreleased, so this feature keeps it (see FR-031).
