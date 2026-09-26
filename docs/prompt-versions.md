# Prompt versions

How to trial, promote and troubleshoot a chunk-summary or HyDE prompt
version. See [ADR-003](adr-003-prompt-versioning.md) for the design and
`specs/002-prompt-versioning/contracts/http-api.md` for the full API
contract. All endpoints here are admin-only and need `DATABASE_URL`
configured (Postgres); every example uses `Authorization: Bearer $TOKEN` for
an admin token.

## Concepts

- **Registry**: every shipped prompt version, immutable, in
  `adapters/llm_api/prompts.py`. A release can add a version; it can never
  change one.
- **Pin**: the version a deployment actually uses. Each operation
  (`chunk_summary`, `hyde`) has one **deployment pin**. `chunk_summary` can
  also have a **per-source override**; `hyde` cannot (it is
  deployment-wide, since a HyDE expansion isn't scoped to one source).
- **Effective version**: a source's override if it has one, otherwise the
  deployment pin.
- **Stale**: a source whose recorded `summary_prompt_version` differs from
  its effective version, or whose `summary_refresh_target` is still set
  (an unfinished refresh — see "Interrupted refreshes" below).
- **`resummarize`**: the background job that brings one source's summaries
  and summary vectors to its target version. It never parses files, embeds
  code or touches the graph.

A release that registers a new prompt version changes nothing by itself —
pins move only when an admin moves them. `GET /prompt-versions` shows every
operation's registered versions and notes, the deployment pin, and every
source's built version, target version and stale flag; the operator UI's
**Prompts** page (next to Backends) is the same information with a dry-run
confirmation built in.

## Trial a version on one source

Set a `chunk_summary` override on the source you want to try the new
version on. Preview first:

```bash
curl -X PUT 'http://localhost:8001/prompt-pins/chunk_summary/sources/<source_id>?dry_run=true' \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"version": 4}'
```

This validates the version and reports what would happen — the source's
current version, target version and chunk count — without writing the
override, notifying any process, or enqueuing a job. Then run it for real
(drop `dry_run`, or set it to `false`):

```bash
curl -X PUT 'http://localhost:8001/prompt-pins/chunk_summary/sources/<source_id>' \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"version": 4}'
```

Only that source becomes stale and gets a `resummarize` job; every other
source keeps using the deployment pin. `PUT /prompt-pins/hyde/sources/{id}`
is refused with a 400 — HyDE has no per-source override.

## Compare

Once the refresh finishes (see "Watch the refresh"), search the trial
source and compare results against sources still on the old version. Both
`GET /prompt-versions` and the Prompts page show the source's recorded
version, its override and whether it's stale.

## Promote to the deployment

If the trial looks good, move the deployment pin. Dry run, then real, the
same way:

```bash
curl -X PUT 'http://localhost:8001/prompt-pins/chunk_summary?dry_run=true' \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"version": 4}'

curl -X PUT 'http://localhost:8001/prompt-pins/chunk_summary' \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"version": 4}'
```

A deployment pin change enqueues a `resummarize` job for every stale source
that has **no** override — an overridden source is left alone; its target
stays its override, so it isn't re-enqueued. Once you're happy with the
promoted version, remove the trial override so the source follows the
deployment pin going forward (`DELETE
/prompt-pins/chunk_summary/sources/<source_id>`); this only enqueues a
refresh if the source's recorded version now differs from the deployment
pin.

Setting the pin to its current version is a no-op — nothing is written.
Setting a `hyde` pin never enqueues a job; the response says the change
"takes effect on the next query", and it does, in every indexer process,
because a pin change is broadcast (`LISTEN prompt_pins_changed`, plus a
`PROMPT_PINS_REFRESH_SECONDS`-second poll — default 5 — as a floor, so
every process picks it up within about 5 seconds even if a notification is
missed).

Setting a pin to an unregistered version is refused with a 400 listing the
valid versions, for both a dry run and a real call.

## Watch the refresh

A pin change response lists `enqueued` (jobs created now), `deferred`
(sources with an active job — see below) and `not_enqueued` (index not
writable — see below), plus a `group_id` when two or more jobs were
created. Watch it the same way as any job group:

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8001/job-groups/<group_id>
```

or a single job's progress (`processed_files`/`total_files` count chunks,
not files, for a `resummarize` job) via `GET /jobs/<job_id>`. Jobs of this
kind show up with `kind: "resummarize"`; a job group triggered by a pin
change has `kind: "prompt-refresh"`. Search keeps working throughout — a
chunk's summary vector may come from either version until its refresh
completes.

### Deferred refreshes

A source can have only one active job. If a stale source already has one
running (an index, incremental, or another refresh), the pin-change
response lists it under `deferred` with the blocking job's ID instead of
enqueuing a second job. When that job finishes — of any kind other than
`resummarize` — the source is re-checked and a refresh is enqueued for it
automatically if it is still stale. A refresh is never enqueued twice for
one source, and a finished refresh never re-enqueues itself, so a refresh
that keeps failing won't loop; see "Retrying a refresh" below.

### Not-enqueued refreshes

If the index isn't writable — `index_status` is `reindex_required`,
`unverified`, or a rebuild is `rebuilding` (see
[docs/upgrading.md](upgrading.md)) — a refresh can't be enqueued for the
affected sources. The pin is still stored (so the intent isn't lost); the
response lists those sources under `not_enqueued` with the reason. They
stay stale until a later refresh. After a rebuild (`POST /index/rebuild`),
every source is fully re-indexed at its target version and becomes current
without a separate refresh.

## Retrying a refresh that ended with errors

If some chunks fail transiently (an LLM timeout or error), the job still
ends `done` but with errors: the source's recorded version does not
change and it stays stale. Request a manual refresh to retry:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8001/sources/<source_id>/resummarize
```

- `202` with a `job_id` and `target_version`: enqueued.
- `200` with `job_id: null` and a `reason`: the source is already current —
  nothing to do.
- `200` with `deferred: true`: another job is active; the refresh runs when
  it finishes.
- `409`: the index isn't writable (same body as any other write route
  under an index guard).

A retry is cheap: summaries generated by the earlier run are cache hits
(`summary_cache`, keyed by `SHA1(chunk_text) + model + prompt_version`), so
only the chunks that actually failed cost a new LLM call; the rest cost
only re-embedding and a write-back if the whole source is redone. There is
no per-row progress marker, so a retry redoes the source, not just the
failed rows — that's what keeps it cheap rather than free.

## Interrupted refreshes

A refresh marks the source (`summary_refresh_target`) with the version it's
moving toward *before* it writes anything, and clears the mark together
with advancing `summary_prompt_version` only on a clean finish. If the
indexer restarts mid-refresh, the interrupted `resummarize` job is
re-enqueued automatically at startup — it isn't treated as superseded by
the source's earlier index job the way other interrupted jobs are. While
the mark is set, the source is reported stale for every target, including
its own recorded version: this is what stops a pin moving back to the
recorded version from hiding a source with mixed summary vectors. Every
pin change that affects such a source enqueues a refresh for it, on top of
the automatic re-enqueue at startup.

## No-op backends

- **`USE_SUMMARY_VECTOR=0`**: a refresh is a no-op that reports "summary
  vectors disabled" and leaves the recorded version unchanged.
- **ChromaDB**: it stores no summary vectors at all, so a refresh there is
  the same reported no-op ("not supported by chromadb").

In both cases the job still finishes `done`, just with nothing to do.

## `file`/`directory` index jobs and the unknown version

Only `repo` index jobs generate summaries (via `_walk_and_index`); `file`
and `directory` jobs insert chunks with no summary vectors. A clean `file`
or `directory` job therefore records the source's `summary_prompt_version`
as unknown (`NULL`), the same as a repo job run with `USE_SUMMARY_VECTOR=0`
or on a store without summary vectors. A source with an unknown recorded
version is never reported stale and never refreshed automatically — there
is nothing to compare against. It becomes tracked once a `repo` job or a
manual `resummarize` populates its summary vectors.

## Running without Postgres

Without `DATABASE_URL`, there are no pins, no per-source tracking and no
refresh jobs. Resolution falls back to the **baseline** versions —
`chunk_summary` v3, `hyde` v1 — the versions a deployment used before this
feature existed, so an upgrade without Postgres changes no prompt. Startup
logs a warning (`prompt pins unavailable without DATABASE_URL; using
baseline chunk_summary v3, hyde v1`), and every endpoint in this doc
returns `503 {"detail": "prompt pins need Postgres (DATABASE_URL)"}`.

## Fail-loud startup after a downgrade

If a stored pin (or override) names a version this build doesn't register
— for example after downgrading to a build that predates that version —
startup **fails**, naming the pin and the versions this build does
register. This is deliberate: silently falling back would mean serving a
different prompt than the one recorded, with no visible warning.

To recover:

- start the newer build that registers that version again, and move the
  pin forward or back to one this build knows before downgrading; or
- edit the `prompt_pins` row directly (`operation`, `scope = 'deployment'`
  or a `source_id`) to a version the running build does register.
