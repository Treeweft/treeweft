# Feature Specification: Index Schema Stamp, Reindex-Required Mode and Rebuild

**Feature Branch**: `001-index-schema-stamp`

**Created**: 2026-09-25

**Status**: Draft

**Input**: User description: "ADR-004 plan 2" — implement ADR-004 §3 (the index stamp,
`index_schema`/`index_status` in the health report, reindex-required mode, the admin rebuild) and
the index-schema contract snapshot deferred from Plan 1 (§4), with the §5 docs and §6 tests that
belong to them.

**Governing record**: [ADR-004](../../docs/adr-004-compatibility-versioning.md) §3, with §1
(health fields), §4 (index-schema snapshot), §5 (docs) and §6 (testing). Where this spec and the
ADR disagree, the ADR wins until it is amended (constitution, "Development Workflow").

## Background

Plan 1 (released as 1.0.0) made the source SemVer and gave `treeweft-mcp` its compatibility
check. The stored index still records nothing about how it was built. If an operator changes the
embedding model or vector dimension, or an upgrade changes the index layout, searches keep
returning results that look confident but are wrong, and nothing tells the operator. This feature
stamps every data store with what built it, compares the stamp at startup, and gives admins a
way to rebuild the index deliberately.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A mismatched index is detected and reported, never searched silently (Priority: P1)

An operator changes the configured embedding model (or vector dimension), or upgrades to a
release whose index schema changed, and restarts the indexer. The indexer notices that the stored
index was built differently, reports `reindex_required` and names the exact difference. Searches
are refused with an explanation instead of returning meaningless results. Agents using
`treeweft-mcp` see that explanation, not "Indexer unreachable". The operator can still sign in,
use the UI and reach the admin functions needed to fix it.

**Why this priority**: This is the defect the feature exists to remove: silent wrong results
after a configuration or upgrade change (constitution Principle V, "Fail loud"). Detection alone,
even before rebuild exists, turns a silent failure into a visible one.

**Independent Test**: Against fake stores, stamp an index with model A, start with model B
configured, and confirm the health report says `reindex_required`, names the store, the field and
both values, that search endpoints are refused with the reason, and that admin, auth and source
endpoints still work.

**Acceptance Scenarios**:

1. **Given** a fresh install with empty stores, **When** the indexer starts, **Then** every store
   is stamped with the current schema version, embedding model and dimension, and the health
   report shows `index_status: ok` and `index_schema: 1`.
2. **Given** stamped stores that match the configuration, **When** the indexer starts, **Then**
   `index_status` is `ok` and all endpoints behave as they do today.
3. **Given** a stored stamp whose embedding model, dimension or schema version differs from the
   configuration, **When** the indexer starts, **Then** `index_status` is `reindex_required` and
   `reindex_reason` names the store, the field, the stored value and the configured value.
4. **Given** `reindex_required`, **When** any client searches, hydrates chunks, explores the graph
   or uses a find endpoint, **Then** the request is refused with a conflict response carrying
   `detail`, `reason` and a pointer to the rebuild procedure.
5. **Given** `reindex_required`, **When** anyone submits a per-source index job (file, directory,
   repo or graph), **Then** the job is refused and no vectors or graph data are written.
6. **Given** `reindex_required`, **When** an admin signs in, opens the UI, lists sources or jobs,
   or calls admin endpoints, **Then** those work normally.
7. **Given** `reindex_required`, **When** an agent calls a `treeweft-mcp` search tool, **Then**
   the agent receives the indexer's reason, not an "unreachable" error.
8. **Given** any index state, **When** a container health check reads the health report, **Then**
   `status` stays `ok`, so containers do not flap.

---

### User Story 2 - Existing (pre-1.0.0) deployments are adopted without a forced re-index (Priority: P1)

An operator upgrades a deployment whose index was built before stamps existed. If the index
really was built with the configured embedding model and dimension, the indexer verifies that,
stamps the stores as schema 1, and carries on. The operator does nothing. If verification shows
a different model or dimension, the index is reported as `reindex_required`. If the embedding
service is not up yet, the index is reported as `unverified` and search keeps working.

**Why this priority**: Every running deployment is unstamped today. If adoption is wrong in
either direction, it forces a needless multi-hour re-index on everyone or lets a mismatched index
through. It must ship together with Story 1.

**Independent Test**: Against fake stores with data and no stamp, plus a fake embedder: (a)
stored vectors re-embed to cosine ≥ 0.99, so the stores are adopted and stamped; (b) they
re-embed differently, so `reindex_required` names the failed check; (c) the embedder is
unreachable, so `unverified`, search allowed, no stamp written.

**Acceptance Scenarios**:

1. **Given** a vector store with data and no stamp whose dimension matches the configuration,
   **When** a sample of three stored chunks re-embeds with the configured model at cosine
   similarity ≥ 0.99 to the stored vectors, **Then** the stores are stamped as schema 1 and
   `index_status` is `ok`.
2. **Given** unstamped data whose stored dimension differs from the configuration, **When** the
   indexer starts, **Then** `index_status` is `reindex_required` with a reason naming the
   dimension check.
3. **Given** unstamped data whose sampled chunks re-embed below 0.99, **When** the indexer
   starts, **Then** `index_status` is `reindex_required` with a reason naming the embedding-model
   check, and nothing is stamped.
4. **Given** unstamped data and an unreachable embedding service, **When** the indexer starts,
   **Then** `index_status` is `unverified`, a warning is logged, search works, no stamp is
   written, and index jobs are refused.
5. **Given** `unverified`, **When** the embedding service becomes reachable, **Then** the next
   background retry or the next index-job submission, whichever comes first, runs verification
   without a restart. The stores are then adopted and stamped (`ok`), or reported
   `reindex_required`. After adoption, the submitted job is accepted.

---

### User Story 3 - An admin rebuilds the index deliberately and watches it recover (Priority: P2)

An admin whose index is `reindex_required` (or who simply wants a clean rebuild) first runs a
dry run that lists every source and its chunk count, changing nothing. The admin then runs the
real rebuild. The index is recreated empty at the current schema and stamped. Every registered
source is re-indexed as one tracked job group. While that runs, `index_status` is `rebuilding`
with sources-done and sources-total, and search is allowed again because the index is consistent,
only incomplete. When the group finishes, the status returns to `ok`. Cached summaries are reused,
so an embedding-model change only re-embeds and does not pay for summaries again.

**Why this priority**: Without it, recovering from `reindex_required` means manual store surgery.
It depends on Story 1's detection, and Story 1 has value on its own before it.

**Independent Test**: Against fakes, call the rebuild dry run and confirm it lists sources and
counts and changes nothing. Call the real rebuild and confirm the stores are recreated and
stamped, one job group is enqueued covering every source, the health report shows `rebuilding`
with progress, search is allowed, and the status becomes `ok` when the group completes.
Non-admins are refused.

**Acceptance Scenarios**:

1. **Given** any index state, **When** an admin requests a rebuild dry run, **Then** the response
   lists each source that would be rebuilt with its current chunk count and the total, and
   nothing changes.
2. **Given** `reindex_required`, **When** an admin requests the real rebuild, **Then** the vector
   collection and the graph data (entities and communities) are dropped and recreated at the
   current schema and stamped, and every registered source is enqueued for re-indexing as one
   job group.
3. **Given** a rebuild in progress, **When** anyone reads the health report, **Then**
   `index_status` is `rebuilding` and `rebuild_progress` gives sources done and total. Search is
   allowed and returns results only from sources rebuilt so far.
4. **Given** a rebuild in progress, **When** the job group completes (including when some sources
   fail), **Then** `index_status` returns to `ok`, and failed sources are visible in the job
   group's results.
5. **Given** a non-admin caller, **When** they request a rebuild or a dry run, **Then** they are
   refused by the admin check.
6. **Given** summaries cached from before the rebuild, **When** sources are re-indexed after an
   embedding-model change, **Then** the cached summaries are reused and only embeddings are
   recomputed.

---

### User Story 4 - Index-schema changes are caught by CI and recorded at release (Priority: P3)

A developer changes the vector-store fields, graph constraints or indexes, the embedded-store
schema, or the SQLite graph tables. The contract test flags this as index-breaking and fails
until `INDEX_SCHEMA_VERSION` is raised and the SemVer major is bumped. The release PR regenerates
the index-schema snapshot, and the changelog entry says "requires re-index".

**Why this priority**: This prevents future silent schema drift. It protects later releases, not
this one, so it can land last.

**Independent Test**: Run the contract test against a synthetic index-schema change and confirm
it is classified index-breaking, names the change and the minimum versions required, and passes
once both versions are raised. Confirm the committed snapshot matches the current code.

**Acceptance Scenarios**:

1. **Given** a committed index-schema snapshot recording schema 1 and source version 1.x,
   **When** the current index schema equals it, **Then** the contract test passes at any
   version.
2. **Given** any difference in the index schema, **When** `INDEX_SCHEMA_VERSION` or the SemVer
   major has not been raised, **Then** the contract test fails, names each change, classifies it
   index-breaking, and states the minimum schema integer and version required.
3. **Given** a release PR, **When** the snapshot-update script runs, **Then** the index-schema
   snapshot is regenerated alongside the API and MCP snapshots.

---

### Edge Cases

- **Stores disagree.** One store matches while the other is mismatched or unstamped. The overall
  `index_status` is the worst per-store result. Order: `reindex_required` > `unverified` >
  `rebuilding` > `ok`. Every mismatched store is named in `reindex_reason`.
- **One store fresh, the other holding unstamped data** (for example the graph was wiped but the
  vectors were not). The fresh store is stamped. The unstamped store follows the legacy-adoption
  rules. An unstamped graph store is adopted only when the vector store's verification passes.
  If the graph has data and the vector store has none, there is nothing to verify against, so the
  result is `reindex_required`.
- **Fewer than three stored chunks** in an unstamped vector store. Verify with the chunks that
  exist. At least one is required. Zero chunks counts as "no data".
- **A stored stamp with a newer schema version** than this release knows (a downgrade). The
  result is `reindex_required`, and the reason says the index was built by a newer schema.
- **Rebuild requested while index jobs are running or queued, or while a rebuild is already in
  progress.** The request is refused with a conflict that names the blocking jobs. Nothing is
  dropped.
- **The indexer restarts mid-rebuild.** The stores are already stamped at the current schema, so
  the stamp check passes. `index_status` is derived from the rebuild job group's state, so it
  shows `rebuilding` while that group is still incomplete.
- **Summary-prompt changes** (ADR-003) never change the stamp or the schema integer, and never
  cause `reindex_required`.
- **No registered sources** at rebuild time. The stores are recreated and stamped, an empty job
  group completes immediately, and the status is `ok`.
- **Simple mode** (embedded vector store and SQLite graph). Stamps, adoption, gating and rebuild
  behave the same as in the full stack.

## Requirements *(mandatory)*

### Functional Requirements

**Stamp and startup check**

- **FR-001**: The system MUST define one index schema integer, `INDEX_SCHEMA_VERSION`, starting
  at 1 and kept with the other version information.
- **FR-002**: Each data store (every supported vector store and graph store) MUST be able to
  record and read back an index stamp holding the schema integer, the embedding model identifier
  and the vector dimension. Where the stamp lives in each store follows ADR-004 §3. The dimension
  MAY be derived from the store's own schema where the store already records it.
- **FR-003**: At startup, after each store's existing setup, the system MUST compare each store's
  stamp with `INDEX_SCHEMA_VERSION`, the configured embedding model and the configured vector
  dimension, and set the index status per the decision table in ADR-004 §3: fresh → stamp, `ok`;
  match → `ok`; mismatch in any field → `reindex_required`; data but no stamp → legacy adoption
  (FR-004).
- **FR-004**: Legacy adoption MUST stamp an unstamped store as schema 1 only after (a) its stored
  dimension matches the configuration and (b) up to three stored chunks re-embed with the
  configured model at cosine similarity ≥ 0.99 to their stored vectors. If either check fails,
  the result MUST be `reindex_required`, naming the failed check.
- **FR-005**: `reindex_reason` MUST name, for each mismatch, the store, the field, the stored
  value and the configured value.

**Health reporting**

- **FR-006**: The indexer's public health report MUST add `index_schema` (the running
  `INDEX_SCHEMA_VERSION`) and `index_status` (`ok`, `unverified`, `reindex_required` or
  `rebuilding`). It MUST include `reindex_reason` when `reindex_required`, and `rebuild_progress`
  (sources done, sources total) when `rebuilding`. Existing fields keep their meaning, and
  `status` stays `ok` in every index state.
- **FR-007**: The UI's health indicator MUST show the index status, and the reason when there is
  one.

**Reindex-required gating**

- **FR-008**: While `reindex_required`, the search, chunk-hydration, graph-explore and find
  (definition, callers, references) endpoints MUST refuse requests with a conflict response
  carrying `detail`, `reason` and a pointer to the rebuild procedure.
- **FR-009**: While `reindex_required`, every job that would write vectors or graph data for a
  source (index file, directory, repo, graph, and any scheduled or fleet-triggered equivalent)
  MUST be refused before it writes anything.
- **FR-010**: While `reindex_required`, authentication, the UI, source and job listing and
  management, and admin endpoints (including the rebuild) MUST keep working.
- **FR-011**: While `unverified`, search MUST work and no stamp may be written until
  verification passes. The system MUST retry the legacy verification (FR-004) before accepting
  any per-source index job, and in the background at a fixed interval, with no restart needed.
  When a retry passes, the stores are stamped and `index_status` becomes `ok`. When a retry
  fails a check, `index_status` becomes `reindex_required`. While verification has still not
  run, index jobs MUST be refused with a conflict response saying the index is unverified and
  the embedding service is unreachable. No vectors from an unverified model may be written into
  unstamped data. (This goes beyond ADR-004's "reruns at the next startup" without conflicting
  with it: a restart still re-runs the check.)

**Rebuild**

- **FR-012**: The system MUST provide an admin-only rebuild operation. With a dry-run option it
  MUST return the sources it would rebuild and their chunk counts, and change nothing.
- **FR-013**: The real rebuild MUST drop and recreate the vector collection and the graph data
  (entities and communities) at the current schema, stamp them, and then enqueue every registered
  source for re-indexing as one job group through the existing fleet mechanism.
- **FR-014**: The rebuild MUST be refused, before anything is dropped, while index jobs are
  running or queued or while another rebuild is in progress.
- **FR-015**: While the rebuild job group is incomplete, `index_status` MUST be `rebuilding`,
  search MUST be allowed, and `rebuild_progress` MUST be reported. The state MUST survive an
  indexer restart. When the group completes, the status MUST return to `ok`.
- **FR-016**: The rebuild MUST keep the summary cache, so re-indexing reuses summaries keyed by
  the LLM model and only recomputes embeddings.
- **FR-017**: Rebuild requests (dry run and real) MUST be logged with the caller, the sources
  affected, and each stage of the real rebuild. The indexer has no admin audit trail to reuse
  (research R8).

**MCP**

- **FR-018**: `treeweft-mcp` tools MUST pass the indexer's reindex-required conflict to the
  agent with its reason. They MUST NOT report it as the indexer being unreachable. Plan 1's error
  mapping is expected to provide this already, and this feature MUST verify it with a test.

**Contract snapshot and CI**

- **FR-019**: A committed index-schema snapshot MUST record the vector-store fields (types, a
  dimension placeholder, indexes, the full-text function), the Neo4j constraints and indexes, the
  embedded vector-store schema, and the SQLite graph tables. It MUST also record the schema
  integer and source version it was taken at.
- **FR-020**: The contract test MUST classify any index-schema difference as index-breaking and
  fail unless `INDEX_SCHEMA_VERSION` exceeds the recorded value and the SemVer major exceeds the
  released major. The failure MUST name each change and the minimums required.
- **FR-021**: The snapshot-update script MUST regenerate the index-schema snapshot along with the
  existing snapshots.

**Docs and governance**

- **FR-022**: The upgrade runbook MUST explain how to read the index fields of the health report,
  what `unverified` and `reindex_required` mean, and how to rebuild (dry run first).
- **FR-023**: ADR-004's status MUST be updated to record §3 as implemented. The constitution's
  Principle VII transition note MUST be updated per Plan 1's hand-off. ADR-003 MUST note that
  summary-prompt changes never bump `INDEX_SCHEMA_VERSION`, if it does not already say so. The
  engineering notes MUST describe the new states.

### Key Entities

- **Index stamp**: what built a store's data: schema integer, embedding model identifier, vector
  dimension. There is one per data store, and it is kept in the store itself so it describes the
  data actually present.
- **Index status**: the indexer-wide state derived from all stores' stamp checks and any active
  rebuild: `ok`, `unverified`, `reindex_required` (with reason) or `rebuilding` (with progress).
- **Rebuild**: an admin-initiated operation. It recreates and stamps the stores, then re-indexes
  every registered source as one job group. Its progress is that group's progress.
- **Index-schema snapshot**: the committed record of the index layout as of the last release,
  used by the contract test to classify changes.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In the end-to-end check, after the stored embedding-model stamp is changed and the
  indexer restarted, 100% of search, hydration, graph-explore and find requests are refused with
  the mismatch reason, and zero return results.
- **SC-002**: An existing 1.0.0 deployment whose index matches its configuration upgrades with
  zero operator action and no re-index: after one restart with the embedding service up, the
  health report shows `ok` and searches return the same results as before the upgrade.
- **SC-003**: An operator reading only the health report and the upgrade runbook can identify
  which store and which setting caused `reindex_required`, and complete a rebuild, without
  reading code or touching a store directly.
- **SC-004**: During a rebuild, the health report's progress reaches sources-total. The status
  returns to `ok` within one status read of the job group completing, and search returns results
  throughout the rebuild for sources already rebuilt.
- **SC-005**: A rebuild after an embedding-model change makes zero new summary-generation calls
  for chunks whose summaries were cached.
- **SC-006**: Any synthetic change in the classifier test table fails the contract test with a
  message naming the change and the required minimum versions. The unit suite stays
  service-free.
- **SC-007**: Container health checks report healthy in every index state, with zero restarts
  caused by `reindex_required` or `rebuilding`.

## Assumptions

- The stamp's placement per store follows ADR-004 §3's table. The ADR marks the embedded vector
  store (LanceDB) placement "to be confirmed during planning". `/speckit-plan` settles it without
  changing the behaviour specified here.
- The embedding model identifier compared is the configured model name. Two differently named
  deployments of the same weights count as different models; the operator can resolve this by
  rebuilding or by aligning names.
- The background re-verification interval while `unverified` defaults to 60 seconds. It is
  short enough that a compose stack whose embedding service starts after the indexer recovers
  within about a minute, and the probe is too cheap to matter. `/speckit-plan` may make it
  configurable.
- Legacy verification samples up to three chunks. The ADR fixes three and the 0.99 cosine
  threshold, and they are not configurable.
- The rebuild re-indexes each registered source from its recorded location and settings, the
  same way a fleet re-index does. A source that can no longer be reached fails in the job group
  and does not block the others.
- Graph data "entities and communities" means everything the graph store holds for indexed
  sources. Users, groups, API keys, source records, jobs and the summary cache live elsewhere and
  are untouched.
- The MCP compatibility check and corrected error mapping from Plan 1 are in place (released in
  1.0.0).
- The new health fields and the rebuild endpoint are additive, a MINOR bump. The API contract
  test requires that bump in the same PR, so this feature sets the version to 1.1.0 (research
  R10). Tagging and regenerating the API and MCP snapshots stay in a separate release PR. This
  feature does not raise `INDEX_SCHEMA_VERSION`.
- Integration tests (stamp read and write against real Milvus and Neo4j) are opt-in, marked
  `slow`, and live under `tests/integration/`. Unit tests use fakes only (constitution
  Principle II).

## Out of Scope

- Minor-level feature negotiation between `treeweft-mcp` and the indexer.
- Automatic rebuilds. A rebuild is always an explicit admin action.
- Partial or per-source rebuilds that mix stamps within one store.
- Migrating vectors between embedding models without re-embedding.
- Changing the Postgres migration runner's error handling (a separate issue per ADR-004).
