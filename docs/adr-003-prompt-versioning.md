# ADR-003: Prompt Versioning with Admin Pins and Summary-Only Refresh

- Status: Accepted and implemented (1.1.0) (amended 2026-09-25 during
  planning of spec `specs/002-prompt-versioning`)
- Date: 2026-09-23
- Owner: indexer / LLM adapter

> An ADR records a decision at a point in time. The Context below describes
> the code as it was on the date above. It was accepted on 2026-09-25. The
> amendments made while planning its implementation are marked "amended
> 2026-09-25" and summarized in "Amendments" at the end.

## Context

Prompt improvements have repeatedly been avoided because shipping one makes
every deployment re-index. Only one product prompt actually forces that:

| Operation | Defined as | Output persisted? | Cost of changing it today |
|---|---|---|---|
| Chunk summary | `_SUMMARY_SYSTEM` + `_SUMMARY_SCHEMA` (`adapters/llm_api/`) | Yes: text in Postgres `summary_cache`, embedding in the vector store's `summary_vector` | Full re-index of every source |
| HyDE | `_HYDE_SYSTEM` + `_HYDE_SCHEMA` | No: in-process `_HYDE_CACHE` only | None, but results change for every deployment at once |

Community summaries are a string template, not LLM output, and the prompt
enhancer makes no LLM calls. The benchmark prompts never touch the index.

Changing the summary prompt today means:

- `PROMPT_VERSION` (`adapters/llm_api/llm_adapter.py`, currently `3`) is a
  code constant. Bumping it turns every `summary_cache` lookup into a miss for
  every deployment on upgrade, whether or not its admin wants the new prompt.
- Nothing records which prompt version produced a stored `summary_vector`, so
  a deployment cannot tell that its index is stale.
- The only way to refresh summaries is a full re-index (parse, chunk, embed
  code, summarize, graph), although only the summary text and its embedding
  change.

Partial precedent exists: the `summary_cache` key `(sha1, model,
prompt_version)` already lets several versions' rows coexist, and
`summary_tail` can read an alternate tier per request. But nothing in the
repository writes a non-current version.

## Decision

Ship prompts as an immutable, versioned registry in code. Each deployment has
an admin-controlled **pin** per operation, and the chunk-summary pin can be
overridden per source. A release that adds a version changes nothing until an
admin moves a pin. Moving the chunk-summary pin triggers a background
**summary-only refresh** (`resummarize`) instead of a re-index.

### 1. Prompt registry

New module `adapters/llm_api/prompts.py`: an append-only registry
`operation -> {version: PromptVersion}`, where `PromptVersion` holds `system`
(text), `schema` (the `ResponseSchema` its output is validated against) and
`notes` (one-line release note).

- Seed versions: `chunk_summary` **v3** is today's `_SUMMARY_SYSTEM` and
  `_SUMMARY_SCHEMA`, which keeps existing `summary_cache` rows valid. `hyde`
  **v1** is today's `_HYDE_SYSTEM` and `_HYDE_SCHEMA`.
- Shipped versions are immutable. A change is a new version number, never an
  edit. A unit test hashes every registered version's `system` text and schema
  against a committed fixture (`tests/unit/fixtures/prompt_hashes.json`), so
  editing a shipped version fails CI and adding one requires adding its hash.
- `PROMPT_VERSION`, `_SUMMARY_SYSTEM`, `_HYDE_SYSTEM`, `_SUMMARY_SCHEMA` and
  `_HYDE_SCHEMA` become registry lookups. The user-message builders (the
  nonce-fenced `_build_summary_user_message`, the HyDE language suffix) and
  `_NO_THINK_SUFFIX` handling stay shared, unversioned code.

**Version resolution.**

- `effective_version("hyde")` is the deployment pin.
- `effective_version("chunk_summary", source_id)` is the per-source override
  if set, otherwise the deployment pin.

Pins are resolved from an in-memory map that is loaded from Postgres at
startup and reloaded on `LISTEN prompt_pins_changed`, the same mechanism as
`embedding_backends`. There is no database round-trip per chunk or query.
Unlike the `embedding_backends` listener, it also reloads after every
(re)connect of its LISTEN connection and every 5 seconds
(`PROMPT_PINS_REFRESH_SECONDS`), so a notification lost while disconnected
delays a change by at most one interval (amended 2026-09-25).

Without Postgres (`DATABASE_URL` unset) there are no pins: resolution uses the
baseline versions `chunk_summary` v3 and `hyde` v1, so an upgrade changes no
prompt, and the pin endpoints return 503 (amended 2026-09-25).

**Caches.**

- `summary_cache` rows are written with the **resolved** version for the call,
  not a constant, so two versions' rows coexist during a migration. The table
  and its key are unchanged; reads stay version-scoped, and rejection markers
  (`""`) are per version.
- The existing per-request `summary_prompt_version` read override
  (`summary_tail`) keeps working unchanged and is not validated against the
  registry (the benchmark reads an unregistered tier). Without it, each tail
  chunk is read at its source's recorded version, falling back to the
  effective version on a miss (amended 2026-09-25).
- `_HYDE_CACHE` keys gain the HyDE version, and the cache is cleared when the
  HyDE pin changes. Otherwise a previous version's expansion would keep being
  served for a repeated query.

### 2. Pin storage and per-source tracking

Migration `021_prompt_versions.sql`:

```sql
CREATE TABLE IF NOT EXISTS prompt_pins (
    operation  TEXT NOT NULL,
    scope      TEXT NOT NULL,          -- 'deployment' or a source_id
    version    INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT,
    PRIMARY KEY (operation, scope),
    CHECK (operation <> 'hyde' OR scope = 'deployment')
);

ALTER TABLE source_records
    ADD COLUMN IF NOT EXISTS summary_prompt_version INTEGER,
    ADD COLUMN IF NOT EXISTS summary_refresh_target INTEGER;
UPDATE source_records SET summary_prompt_version = 3
    WHERE summary_prompt_version IS NULL;
```

- The migration does **not** insert pin rows; see "Pin seeding" below.
- `source_records.summary_prompt_version` is the version the source's stored
  summary vectors were built with. It is set when a full index job (file,
  directory or repo) completes cleanly, meaning no failed files **and** no
  transient summary failures, to the version resolved when that job started;
  and when a `resummarize` job completes cleanly. An incremental index
  job never changes it: it re-summarizes only the changed files, so advancing
  the version would report a stale source as current (amended 2026-09-25,
  spec 002 FR-010). A clean full job with summary vectors off, or on a store
  without them, sets it to `NULL`, as do file and directory jobs, which insert
  chunks without summary vectors (only repo jobs summarize). A graph-only job never changes it. `NULL` means
  unknown or never summarized (for example, indexed with
  `USE_SUMMARY_VECTOR=0`). Backfilling 3 is correct because every existing
  index was built with v3. The backfill also applies to sources indexed with
  `USE_SUMMARY_VECTOR=0`; that is accepted: their summary vectors are zero
  vectors, so recording v3 is harmless, and a later refresh would populate
  them. This avoids a per-source scan when the migration runs.
- Deleting a source deletes its override row, in the source-delete path. There
  is no foreign key because `scope` is polymorphic.
- Mutations emit `NOTIFY prompt_pins_changed` in the same transaction as the
  row write.

- `source_records.summary_refresh_target` is the version an unfinished
  `resummarize` was moving the source toward. It is set when the job starts,
  before its first write, and cleared together with setting
  `summary_prompt_version` when the job, or a full index job, completes cleanly.
  While it is set, the source's summary vectors may be mixed (amended
  2026-09-25, spec 002 FR-015).

**Staleness.** A source is stale when `summary_refresh_target IS NOT NULL`,
or when `summary_prompt_version IS NOT NULL` and it differs from
`effective_version("chunk_summary", source_id)`. The first rule keeps a source
with a partly applied or overtaken refresh from looking current: without it,
moving the pin back to the recorded version would hide the mixed vectors, and
a manual refresh would be a no-op. Every pin change that affects such a source
enqueues a refresh for it. During a migration:

- new and incremental indexing of the source already uses the target version;
- search uses whatever vectors are stored. Every version's summaries are
  embedded with the same embedding model, so mixed versions stay comparable;
- the recorded version advances only when a refresh completes cleanly, so the
  source stays stale until every chunk is refreshed.

**Validation.**

- Setting a pin to a version that is not in the registry returns `400` and
  lists the valid versions.
- A per-source override for `hyde` returns `400`; the table's `CHECK` enforces
  the same rule.
- If a stored pin names a version this build does not know (after a
  downgrade), startup fails loudly, naming the pin and the available versions.

### 3. The `resummarize` job

A new job kind, dispatched on `Job.kind` like the others. Migration 008's
one-active-job-per-source rule does not queue a second job behind an active
one; it refuses it. So a refresh for a source with an active job is
**deferred**: the triggering response lists it with the blocking job, and when
any job other than a `resummarize` finishes, the source is re-checked and a
refresh is enqueued if it is still stale. A finished refresh re-triggers only
when a pin change overtook it, so a refresh that keeps failing cannot loop. Startup crash recovery re-enqueues an interrupted `resummarize`
instead of treating it as superseded by the source's earlier index job
(amended 2026-09-25).

Per source:

0. **Mark** the source: set `source_records.summary_refresh_target` to the
   target version before anything is written.
1. **Snapshot** the source's primary keys (IDs only,
   `consistency_level=Strong`). This is required because on Milvus 2.5 an
   upsert re-inserts a row under a **new** primary key (see "Verified Milvus
   behaviour"), so iterating live rows would revisit rewritten ones.
2. For each batch of IDs, using the existing embed batch size:
   - fetch the full rows (`output_fields=["*"]`). Rows already carry
     `chunk_text`, `language` and `file_path`, which the summary prompt needs;
   - resolve summaries at the source's target version through the existing
     `_summaries_for_chunks` path, reusing cache hits and rejection markers;
   - embed the summaries through the existing embedding proxy;
   - upsert each full row **without** `sparse_vector`, which the BM25 function
     generates server-side, carrying the new `summary_vector`. A chunk with no
     summary gets the store's own no-summary value, as at index time: a zero
     vector on Milvus, NULL on LanceDB (amended 2026-09-25).
   After the last batch, a `Strong` count of the source's rows must equal the
   snapshot size, or the job fails loudly (amended 2026-09-25).
3. On completion, set `source_records.summary_prompt_version` to the target
   and clear `summary_refresh_target`, in one statement, **only if** every
   chunk got a summary or a rejection marker. If any chunk failed transiently
   (a timeout or LLM error), the job finishes `done` with errors and both
   columns are left unchanged, so the source stays stale and can be retried.
   The same applies to an interrupted or failed job.

**Retry.** There is no per-row progress marker; re-running redoes the source.
Summaries generated by the earlier run are cache hits, so a retry costs only
the embedding and the write-back. Progress is reported as chunks processed out
of the total, using the existing job fields.

**Simple mode (LanceDB).** `summary_vector` is updated in place, with no key
churn. `USE_SUMMARY_VECTOR` defaults to 0 there; with it off, `resummarize`
is a no-op that leaves `summary_prompt_version` unchanged and reports that
summary vectors are disabled. LanceDB rows are updated with `merge_insert` by
their client-generated `id`.

**ChromaDB.** The ChromaDB backend stores no summary vectors
(`insert_chunks` discards them), so `resummarize` there is the same reported
no-op (amended 2026-09-25).

**Triggers.**

- Changing the deployment `chunk_summary` pin enqueues `resummarize` for every
  stale source that has no override.
- Setting or removing an override enqueues it for that source if it is stale.
- An admin can enqueue it manually for one source.
- A source already at its target version is a no-op.

**Verified Milvus behaviour** (2026-09-23, Milvus 2.5.4, pymilvus 3.0.0), on a
throwaway collection with the production schema shape (auto-ID `INT64` primary
key, analyzed `chunk_text`, `vector`, `summary_vector`, a BM25 function
producing `sparse_vector`, dynamic fields enabled):

- A partial upsert (`{id, summary_vector}` only) is **rejected** with "Insert
  missed an field `chunk_text`". Partial update arrives in Milvus 2.6.
- A full-row upsert (every field except `sparse_vector`) **succeeds**. With
  strong consistency exactly one row remains; it has the new `summary_vector`,
  the dynamic field survives and BM25 search finds it. Its primary key
  **changes**.
- A read immediately after an upsert at the default consistency level returned
  the stale row, so the job must use `Strong` for its snapshot and its
  verification reads.

Milvus row IDs are not referenced outside the vector store: `hydrate_chunks`
identifies hits by file path and line range. The implementation must confirm
that no other consumer (search audit, for example) stores Milvus IDs before
relying on this.

### 4. Upgrades, admin API and UI

**Pin seeding.** This is what makes "pinned until the admin opts in" hold. At
startup, for each operation with no `deployment` pin row:

- if `source_records` is empty (a fresh install), seed the **latest**
  registered version;
- if sources exist, seed the version they were built with: the most common
  non-NULL `source_records.summary_prompt_version` for `chunk_summary` (3 for
  every deployment that predates this ADR), and v1 for `hyde`.

After seeding, a release that registers new versions changes nothing; only an
admin moves a pin.

**Admin API.** Every endpoint uses `authz._require_admin`, and mutations
record `updated_by`.

- `GET /prompt-versions`: the registry (per operation: versions, notes, the
  latest), the current pins (deployment and overrides), and per source its
  `summary_prompt_version`, effective target and `stale` flag.
- `PUT /prompt-pins/{operation}` with `{"version": N}`: sets the deployment
  pin. The response lists the `resummarize` jobs it enqueued.
- `PUT /prompt-pins/chunk_summary/sources/{source_id}` with `{"version": N}`,
  and `DELETE` on the same path: set or clear an override. The response lists
  any job enqueued.
- **Dry run.** Each of the three mutating pin endpoints accepts
  `?dry_run=true`. It validates the request exactly as a real call would, then
  returns what the call *would* do and writes nothing: no pin row, no
  `NOTIFY`, no job. The response has the same shape as a real call, with
  `"dry_run": true` and, in place of enqueued job IDs, the sources that would
  get a `resummarize` job, each with its current and target version and its
  `chunk_count` from `source_records`, plus the total chunk count. The chunk
  count is an upper bound on LLM calls: the dry run does not read chunk texts,
  so it cannot say how many would be summary-cache hits. A `hyde` pin change
  reports that it takes effect on the next query and enqueues nothing.
- `POST /sources/{source_id}/resummarize`: a manual refresh.

**Operator UI.** A **Prompts** page next to Backends: per operation, the
versions with their notes and the deployment pin selector; and a sources table
with each source's built version, target version, a stale badge, an override
control and a refresh action. Changing a pin in the UI first calls the dry run
and shows its result (sources affected, total chunks) in a confirmation step;
only confirming sends the real request.

**Docs.**

- `engineering-notes.md`: replace "bump `PROMPT_VERSION` when prompts change"
  with "register a new version in `prompts.py`; never edit a shipped one", and
  describe the rollout flow.
- A new runbook, `prompt-versions.md`: trial a version on one source with an
  override, compare, promote the deployment pin, and watch the refresh jobs.
- Release notes list newly registered prompt versions.

### 5. Testing

Unit tests (no external services):

- the registry immutability hash fixture, including that adding a version
  requires its hash;
- resolution precedence (override over deployment pin) and pin validation
  (unknown version, HyDE override);
- `summary_cache` writes use the resolved version; the HyDE cache key includes
  the version and the cache is cleared when the HyDE pin changes;
- the startup seeding rules (fresh install versus existing sources), and that
  an unknown pinned version fails startup;
- which sources a pin change enqueues (a deployment change skips overridden
  sources; a source already at its target is a no-op);
- dry run: it reports exactly the sources and chunk totals the real call would
  enqueue, applies the same validation (`400` for an unknown version or a HyDE
  override), and writes no pin row, emits no `NOTIFY` and enqueues no job;
- `resummarize` against a fake vector store: snapshot then batch, a zero
  vector for missing summaries, and the recorded version advancing only on a
  clean run;
- the LanceDB in-place update on a real temporary table.

Integration test (opt-in, marked `slow`, skipped unless `MILVUS_TEST_URI` is
set; it uses a per-run collection and drops it afterwards): after
`resummarize`, the row count is unchanged, there are no duplicates, the new
`summary_vector` values are present, BM25 and dynamic fields are intact, and
dense and BM25 search still return the chunks.

End-to-end check against the live stack: index a source at v3, register a
test-only v4, move the deployment pin, and watch the source go stale, get
refreshed and become current. Verify that the code `vector` values are
unchanged and that no parsing or code embedding ran (job counters, embedding
metrics).

## Rejected alternatives

### Pins as environment variables, with a CLI backfill script

`SUMMARY_PROMPT_VERSION` and `HYDE_PROMPT_VERSION` in `.env`, per-source
overrides as a JSON environment variable, and a script to run the refresh.
Fewer moving parts, but changing a pin needs a restart, per-source overrides
are awkward to manage, and there is no API or UI visibility into which sources
are stale. The embedding-backends registry already moved off `.env` for the
same reasons.

### Store the prompt version on every stored vector

Tag each vector-store row with its prompt version, enabling query-time
filtering and side-by-side versions. It needs a collection schema change
(drop and re-index every source once) and more storage, for side-by-side A/B
capability that is not a goal here. The per-source `summary_prompt_version`
plus version-scoped `summary_cache` rows give the needed visibility without
it.

### New versions become active automatically on upgrade

A release makes its newest prompt the default and a background refresh
migrates every source. This removes the admin's control over when LLM cost is
spent and over whether a new prompt is adopted at all, which is the problem
this ADR exists to solve.

### Admin-authored prompts stored in Postgres

Admins could create custom versions through the API. This needs validation
that a custom prompt fits its validator schema, auditing, and care to keep the
prompt-injection defenses (the nonce fence) intact. Shipped, reviewed and
tested versions cover the goal. This can be revisited on top of the same pin
mechanism.

## Consequences

### Positive

- A prompt improvement can ship in any release without forcing work on any
  deployment.
- Switching the summary prompt costs LLM calls for uncached chunk texts plus
  summary embeddings and a write-back: no parsing, no code embeddings, no graph
  work.
- Admins can trial a version on one source before promoting it, can see
  exactly which sources are stale, and can preview a pin change's scope with a
  dry run before any LLM work starts.
- Prompt text becomes reviewable history: a shipped version cannot be changed
  silently.

### Negative

- A new table, a new column, a new job kind, four admin endpoints and a UI
  page to maintain.
- On Milvus 2.5 the refresh rewrites whole rows and changes their primary
  keys. It is correct, but it costs more I/O than a partial update, and it
  depends on nothing outside the vector store holding Milvus IDs.
- During a migration a source's summary vectors come from mixed prompt
  versions. They share an embedding model, so they stay comparable, but
  retrieval quality in that window is a blend of the two prompts.
- Every new prompt version needs a registry entry, a hash fixture update and
  release notes, rather than a one-line constant edit.

## Out of scope

- Admin-authored or custom prompts.
- Side-by-side A/B coexistence of versions for the same chunk.
- Per-row version tags in the vector store.
- Per-source HyDE pins (a cross-repo query spans sources).
- Versioning the benchmark prompts (`application/benchmark/*`).

## Resolved questions

Both questions raised when this was drafted were settled on 2026-09-24:

1. **Sources indexed with `USE_SUMMARY_VECTOR=0` record v3** from the
   migration backfill, rather than `NULL` (see §2).
2. **Moving a pin supports a dry run** (`?dry_run=true`), and the operator UI
   always shows it as a confirmation step (see §4).

A summary-prompt version change (`PROMPT_VERSION`) never bumps
`INDEX_SCHEMA_VERSION` and never causes `reindex_required` — it is
versioned and refreshed entirely through this record's own
`summary_prompt_version` mechanism, orthogonal to the vector/graph index
schema ADR-004 §3 stamps and checks (`src/treeweft/domain/index_stamp.py`).

## References

- `src/treeweft/adapters/llm_api/llm_adapter.py`: `PROMPT_VERSION`, the prompt
  constants, `cache_get_many` and `cache_put`, `_HYDE_CACHE`.
- `src/treeweft/adapters/llm_api/llm_caller.py`: `_SUMMARY_SCHEMA`,
  `_HYDE_SCHEMA`.
- `src/treeweft/adapters/postgresql/migrations/005_summary_cache.sql`: the
  cache key.
- `src/treeweft/adapters/postgresql/migrations/007_embedding_backends.sql`:
  the admin-registry and `LISTEN`/`NOTIFY` pattern this ADR follows.
- `src/treeweft/adapters/milvus/vector_store.py`: the collection schema.
- `src/treeweft/application/indexer_runners.py`: `_summaries_for_chunks`,
  `_process_file`.
- [ADR-001](adr-001-cost-aware-embedding-proxy.md): the embedding proxy that
  the refresh job reuses.

## Amendments

Made on 2026-09-25 while planning the implementation
(`specs/002-prompt-versioning/research.md`, where each has its evidence):

1. Incremental index jobs never advance a source's recorded version; only a
   clean full index job or a clean refresh does, and "clean" includes no
   transient summary failures (§2; research R6).
2. `summary_refresh_target` marks an unfinished refresh, so a source with
   mixed summary vectors stays stale for every target (§2, §3).
3. Refreshes are deferred behind an active job and enqueued when it finishes,
   rather than serialized by migration 008, which it does not do (§3; R4).
   Interrupted refreshes are re-enqueued at startup (R5).
4. The ChromaDB backend has no summary vectors, so a refresh there is a
   reported no-op, and "zero vector" is each store's no-summary value (§3; R7).
5. Pin propagation reloads after a reconnect and on a 5-second interval (§1;
   R3). Without Postgres, the baseline versions are used (§1; R9).
6. The summary tail's default read version is the source's recorded version
   (§1; R8).
7. A post-refresh row count check guards against duplicates from Milvus
   re-keying (§3; R10).
8. After code review (2026-09-26): a transient summary failure during a
   refresh keeps the chunk's existing summary vector (only a rejection gets the
   no-summary value), and a run in which every chunk fails ends `failed`. A
   refresh re-reads the pins before resolving its target, re-checks the source
   around each write so a deleted source is not resurrected by a Milvus
   upsert, and a refresh overtaken by a pin change is followed by one to the
   new target.
9. After code review (2026-09-26): index work preempts an active refresh
   instead of being deduplicated against it. A queued refresh is cancelled; a
   running one gets a `waiting` index job behind it and stops at its next
   batch. Waiting jobs run in arrival order, and the refresh is re-enqueued if
   the source is still stale. Migration 022 indexes waiting jobs.
