# ADR-004: SemVer Source, CalVer Product, and a Stamped Index Schema

- Status: Accepted and fully implemented (§1, §2 and the release/docs parts of §5 in 1.0.0; §3, §4's index-schema snapshot and the remaining §5 docs in 1.1.0)
- Date: 2026-09-24
- Owner: release process / indexer / MCP server

> An ADR records a decision at a point in time. The Context below describes
> the code as it was on the date above. This record is **Accepted** and
> fully implemented. §1, §2 and the release/docs parts of §5 shipped in
> 1.0.0; §3 (the index stamp, reindex-required mode and rebuild), §4's
> `contracts/index_schema.json` snapshot, and the rest of §5 shipped in
> 1.1.0. §3's design extends beyond what this record specifies to cover
> several indexer processes sharing one Postgres (a supported deployment
> topology this record did not originally address) via a Postgres
> session-level advisory lock; see `specs/001-index-schema-stamp/`
> (research R13) for that design.

## Context

Treeweft ships one CalVer number, `YYYY.M.D` (`docs/docker-images.md`). It is
the `pyproject.toml` version, the git tag (`v2026.9.23`) and the image tags
(`2026.9.23`, the rolling month tag `2026.9`, and `latest`). The publish
workflow's `check-tag` job requires the tag to equal the `pyproject.toml`
version.

A date tells an operator *when* a release was cut. It cannot tell software
whether two components can talk to each other, or tell an operator whether an
upgrade will force a re-index. Three contracts need that:

- **The indexer HTTP API** (`/search`, `/index-*`, `/graph-explore`, `/jobs`,
  …). Its clients are the `treeweft-mcp` stdio server, the operator UI, the
  fleet CLI and the benchmark harness. `treeweft-mcp` and the indexer can come
  from different checkouts or images.
- **The MCP tool surface**: tool names and parameter schemas, which agent
  prompts and client configurations depend on. The MCP server is a thin proxy
  over the indexer API.
- **The stored index**: the vector-store collection, the graph, and what built
  them (embedding model, vector dimension).

What the code did on the date above:

- The version exists only in `pyproject.toml`; nothing reads it at runtime.
  `GET /health` returns `{"status", "database", "auth_enabled"}`, and no API,
  MCP or schema version is reported anywhere.
- `treeweft-mcp` never contacts the indexer at startup. Every failure,
  including `4xx` and `5xx` responses from a changed endpoint, reaches the
  agent as `"Indexer unreachable: …"`.
- Neither the embedding model nor `VECTOR_DIM` is recorded with the stored
  index. On the Milvus path, `init_collection` returns early when the
  collection exists, so a changed dimension is not detected by Treeweft; only
  the LanceDB adapter checks the dimension at startup.
- `docs/docker-images.md` offers the month tag as a pin ("or a month:
  `TREEWEFT_IMAGE_TAG=2026.9`") but makes no stability promise. There is no
  CHANGELOG.
- The Treeloom back-compat removal scheduled for 2026-11-01 (issue #14) removes
  only the `TREELOOM_*` → `TREEWEFT_*` environment-variable aliases and the
  rewriting of legacy benchmark result names. No API, field, MCP tool or server
  alias exists; those were renamed outright earlier. It affects operator
  configuration, not API or MCP clients.

## Decision

1. **The product is CalVer.** Published images are named by release date:
   `YYYY.M.D` (`.N` for a same-day re-release), the rolling `YYYY.M`, and
   `latest`. A CalVer git tag (`v2026.10.1`) triggers publishing, as today.
2. **The source is SemVer.** `pyproject.toml` carries `MAJOR.MINOR.PATCH`,
   starting at **1.0.0**, and each release commit carries a SemVer git tag
   (`v1.2.0`) as well as the CalVer tag. MAJOR covers breaking changes to the
   indexer API, the MCP tool surface or the index schema; MINOR covers
   additions; PATCH covers fixes. The MCP tool surface shares the source
   version while the MCP server stays a thin proxy.
3. **The index has its own schema integer**, `INDEX_SCHEMA_VERSION`, starting
   at 1. It is stamped into each data store together with the embedding model
   and dimension, and checked at startup. Bumping it requires a SemVer MAJOR
   bump. Summary-prompt changes never bump it: ADR-003's summary-only refresh
   handles them.
4. **SemVer is enforced, not remembered**, by committed contract snapshots
   and a unit test that classifies changes against the last release.
5. **The month tag is a stability pin**: breaking changes land only in the
   first release of a calendar month, enforced in the publish workflow.

### 1. Versions and how they are reported

New module `src/treeweft/versions.py`, the single source of truth:

- `SOURCE_VERSION` is read from package metadata
  (`importlib.metadata.version("treeweft")`), so `pyproject.toml` is the only
  place it is written. `SOURCE_MAJOR` is derived from it.
- `INDEX_SCHEMA_VERSION = 1`.
- `PRODUCT_RELEASE` is read from `TREEWEFT_RELEASE`, which the Dockerfiles set
  at image build time from the CalVer tag. It is `None` when running from a
  source checkout, which is the normal way to run the indexer (on the host);
  nothing outside a published image knows the product release.

`GET /health` on the indexer stays public and gains fields; the existing
fields keep their meaning:

```json
{"status": "ok", "database": "connected", "auth_enabled": false,
 "version": "1.0.0", "release": "2026.10.1", "index_schema": 1,
 "index_status": "ok"}
```

- `index_status` is `ok`, `unverified`, `reindex_required` or `rebuilding`
  (§3). With `reindex_required`, a `reindex_reason` field names what differs.
  With `rebuilding`, a `rebuild_progress` field gives sources done and total.
- `status` stays `"ok"` in every index state, so container health checks do not
  flap during a rebuild. The UI's health indicator also shows `index_status`.

Also:

- The MCP HTTP app's `/health` reports the same `version` and `release`.
- Both FastAPI apps are created with `version=SOURCE_VERSION`, so the OpenAPI
  document states it.
- Images carry the OCI labels `org.opencontainers.image.version` (the CalVer
  release) and `org.opencontainers.image.revision` (the commit), plus
  `treeweft.source-version` (the SemVer).
- `pyproject.toml` moves from `2026.9.23` to `1.0.0`, and `uv.lock` follows.
  `tests/unit/test_version_scheme.py` asserts SemVer instead of CalVer.

### 2. The MCP client compatibility check

`application/mcp_server.py` gains one helper, `_ensure_compatible()`, called at
the start of every tool before its first indexer request. Every tool already
goes through `INDEXER_URL`, so this is a single choke point.

- **First call:** `GET {INDEXER_URL}/health` (public, no auth). Compare the
  major of its `version` with the client's `SOURCE_MAJOR`, and cache the result.
- **Match:** the tool proceeds. The result is cached for 5 minutes, so an
  indexer upgraded mid-session is noticed.
- **Major mismatch:** the tool returns immediately, without calling its
  endpoint, a structured error of the same shape as today's errors, e.g.
  `{"error": "Incompatible indexer: indexer is 2.1.0, this treeweft-mcp is
  1.4.0 (major versions must match). Upgrade treeweft-mcp to 2.x, or run an
  indexer 1.x."}`
- **No `version` in `/health`** (an indexer older than 1.0.0): a clear error
  saying the indexer predates version reporting and needs upgrading.
- **Indexer unreachable:** nothing is cached; the tool returns the normal
  "unreachable" error and the next call checks again. Tools never disappear
  from the session.

Error mapping is split. Today every `httpx.HTTPError`, including `4xx` and `5xx`
status errors, is reported as "Indexer unreachable". After this change,
connection errors stay "unreachable", and HTTP status errors report the status
code and the indexer's `detail`. Without this, the `409` from
reindex-required mode (§3) would still look like an outage to the agent.

Only the major is compared. An older indexer minor could lack an endpoint a
newer client calls; that surfaces as a `404`, which the corrected error mapping
reports clearly. Minor-level feature negotiation is out of scope.

### 3. The index stamp and reindex-required mode

`IndexStamp(schema: int, embedding_model: str, vector_dim: int)`. The vector-
and graph-store ports both gain `read_stamp()` and `write_stamp()`. The graph
store is included because community embeddings are stored in the graph, so
they depend on the embedding model too.

| Store | Where the stamp lives |
|---|---|
| Milvus | Collection properties `treeweft.index_schema` and `treeweft.embedding_model`; the dimension is read from the collection schema itself |
| LanceDB | Table schema metadata (feasibility to be confirmed during planning) |
| Neo4j | One `(:TreeweftMeta {id: "index"})` node |
| SQLite graph | A one-row `treeweft_meta` table |

Stamping the stores themselves, rather than recording the stamp in Postgres,
means the check describes the data that is actually there, even when a store
is replaced, wiped or restored independently of Postgres.

At startup, after each store's existing setup, the indexer compares each
store's stamp with `INDEX_SCHEMA_VERSION`, the configured `EMBEDDING_MODEL` and
`VECTOR_DIM`:

- **No data and no stamp** (a fresh install): write the stamp; `ok`.
- **Stamps match:** `ok`.
- **Data but no stamp** (every deployment older than 1.0.0): adopt it as
  schema 1 and stamp it, but only after verifying the embedding model instead
  of assuming it:
  - the dimension must match the Milvus collection schema;
  - three stored chunks are re-embedded with the configured model and compared
    with their stored vectors; cosine similarity of at least 0.99 counts as the
    same model.

  If either check fails: `reindex_required`, naming the failed check. If the
  embedding service is not reachable yet: `unverified`. Search works, the stamp
  is not written, a warning is logged, and the check reruns at the next
  startup.
- **Mismatch in any field:** `reindex_required`, with a reason naming the store,
  the field, and the stored and configured values.

In `reindex_required` mode:

- `/search`, `/hydrate-chunks`, `/graph-explore` and the find endpoints return
  `409` with `detail`, `reason` and a pointer to the rebuild.
- Per-source index jobs are refused, so new vectors never mix into an
  incompatible collection.
- Authentication, the UI, sources, jobs and admin endpoints keep working.

**Rebuild.** New admin-only `POST /index/rebuild`, with `?dry_run=true` (as in
ADR-003) returning the sources and chunk counts it would rebuild without
changing anything. The real call:

1. drops and recreates the vector collection and the graph data (entities and
   communities) at the current schema, and stamps them;
2. re-indexes every source in `source_records` as one job group, using the
   existing fleet mechanism.

Once the new stamp matches, `index_status` is `rebuilding`, with progress.
Search is allowed again, because the index is consistent, only incomplete, and
`/health` says so. It returns to `ok` when the job group completes. The summary
cache is kept: summaries are keyed by the LLM model, so an embedding-model
change reuses them and only re-embeds.

### 4. Contract snapshots and CI enforcement

A new top-level `contracts/` directory holds snapshots of the surface **as of
the last release**. Each file records the source version (and, for the index,
the schema integer) it was taken at:

- `contracts/api.json`: the indexer's OpenAPI document reduced to what clients
  depend on: paths, methods, parameters, request and response schemas, and
  required fields. Descriptions and examples are dropped, so wording edits are
  not changes.
- `contracts/mcp_tools.json`: each MCP tool's name and parameter schema, as
  FastMCP publishes them.
- `contracts/index_schema.json`: the vector-store fields (types, a dimension
  placeholder, indexes, the BM25 function), the Neo4j constraints and indexes,
  the LanceDB schema and the SQLite tables.

The snapshots are regenerated only in a release PR. If each feature PR
regenerated them, two additive PRs in one release cycle would force 1.1.0 and
then 1.2.0 before anything shipped.

`tests/unit/test_contracts.py` builds the current surface in memory, compares
it with the snapshots, and classifies the difference:

| Change | Classification | Requirement |
|---|---|---|
| Removed endpoint, method or tool; removed or renamed parameter or response field; optional → required; type change | Breaking | `pyproject` major > released major |
| New endpoint or tool; new optional parameter or response field | Additive | `pyproject` > released version at minor level or above |
| Any change to the index schema | Index-breaking | `INDEX_SCHEMA_VERSION` > recorded value, and a major bump |
| Nothing | — | any version |

A failure names each change, its classification and the minimum version
required.

**Release procedure:**

1. A release PR sets the `pyproject.toml` SemVer to what the test demands,
   regenerates the snapshots (`scripts/update_contracts.py`) and adds a
   `CHANGELOG.md` entry. The snapshot diff drafts the entry's API section, and
   the entry says "requires re-index" whenever `INDEX_SCHEMA_VERSION` changed.
2. After the merge, push the SemVer tag (`v1.2.0`) first, then the CalVer tag
   (`v2026.10.1`) on the same commit.

**The publish workflow's `check-tag` job** (its logic moves into
`scripts/check_release_tags.py` so it can be unit-tested):

1. The CalVer tag is well-formed (as today).
2. The same commit carries `v<pyproject version>`.
3. **Month rule:** if an earlier CalVer tag exists in the same month, the
   SemVer major on its commit must equal this release's major. A schema change
   requires a major bump, so this covers the index too. Otherwise the job fails:
   "breaking release mid-month: publish it as the first release of next month".

### 5. Docs and related updates

- `docs/docker-images.md`: the "Versioning" section is rewritten (CalVer
  images, SemVer source, both tags per release with SemVer first, and the
  month-tag promise with its enforcement), and the release procedure follows
  §4.
- New `CHANGELOG.md`: one entry per release, headed with both versions
  (`1.2.0 — 2026.10.1`), with sections "Breaking", "Requires re-index",
  "Operator configuration", "Added" and "Fixed".
- The Treeloom cutoff is recorded under "Operator configuration" for the first
  release on or after 2026-11-01, as a removal of the `TREELOOM_*` environment
  aliases. It is not a SemVer major. Issue #14 is updated to say so.
- New runbook `docs/upgrading.md`: reading `/health`, what `reindex_required`
  means, and how to rebuild (dry run first).
- The constitution's versioning principle is amended to match this record, and
  ADR-003 gains a note that summary-prompt changes never bump
  `INDEX_SCHEMA_VERSION`.

### 6. Testing

Unit tests (no external services):

- the versions module and the `/health` fields;
- the MCP check: match, mismatch, an indexer too old to report a version,
  unreachable (nothing cached), the 5-minute cache, a `409` passed through, and
  the corrected error mapping;
- the stamp decision table with fake stores: fresh, match, each field
  mismatching, and the legacy cases (adopted when verification passes,
  `reindex_required` when it fails, `unverified` when embedding is down);
- reindex-required gating: the listed endpoints return `409` while admin
  endpoints keep working;
- rebuild, dry run and real, against fakes;
- the contract-difference classifier, with a table of synthetic schema
  changes, and the snapshot test itself;
- `scripts/check_release_tags.py` against a temporary local git repository.

Integration tests (opt-in, marked `slow`, real services): stamp read and write
in Milvus (collection properties and the schema's dimension) and in Neo4j (the
metadata node).

End-to-end check on the live stack: start at 1.0.0 and read `/health`; edit the
stored Milvus stamp to simulate an embedding-model change and restart; confirm
`reindex_required` and the `409`s; run the rebuild dry run and then the
rebuild; watch `rebuilding` become `ok` and search work again.

## Rejected alternatives

### Separate MAJOR.MINOR versions for the API and the MCP surface, with CalVer source

The source stays CalVer and each contract gets its own version constant. This
adds numbers that must each be bumped correctly, when one SemVer for the source
tree already describes both: the indexer and `treeweft-mcp` are built from the
same source, so their majors are the compatibility signal.

### SemVer git tag triggers publishing; no CalVer tag

A SemVer tag alone publishes images named by the build date. The product's own
releases then have no git tag, and a CalVer image could not be traced to a tag
without reading its labels. Both tags on the release commit keep both version
lines addressable in git.

### Record the index stamp in Postgres

One row in Postgres is simpler and easier to report on, but it describes the
data Postgres expects rather than the data that exists. It goes wrong silently
whenever a store is replaced, wiped or restored independently, or `MILVUS_URI`
is pointed at another instance.

### Use the source major as the index stamp

Stamping the SemVer major into the stores removes one number, but every
API-only breaking change would then force a full re-index.

### Refuse to start, or warn and keep serving, on an index mismatch

Refusing to start locks admins out of the API and UI they need to rebuild.
Warning and serving returns confident-looking wrong results, which the
constitution's fail-loud principle forbids.

### Check versions at MCP startup and exit on mismatch

An indexer that is only down or restarting when an agent launches would remove
Treeweft's tools for the whole session. The lazy per-call check reports the
same mismatch without that failure mode.

### Automatic versioning from commit messages (semantic-release, release-please)

These tools require conventional commits, which the history does not use, and
cannot see an API or schema change that a commit message does not mention. The
contract snapshots detect changes from the code itself.

### Separate versions for each image

`indexer`, `mcp-server`, `ui` and `qwen3-reranker` ship together from one
repository on one tag. Per-image versions would add release overhead and a
compatibility matrix that no consumer asks for. The interfaces between
components are versioned, not the components.

## Consequences

### Positive

- `treeweft-mcp` detects an incompatible indexer and tells the agent exactly
  what to upgrade, instead of failing mid-session with misleading errors.
- A change of embedding model, vector dimension or index schema can no longer
  silently produce wrong search results; the indexer names the mismatch and
  offers a rebuild through its API.
- A surprise re-index becomes visible before an upgrade: the CHANGELOG says
  "requires re-index", the schema integer changes, and a major version bump
  accompanies it.
- The month tag becomes a pin operators can rely on.
- SemVer bumps are checked by CI against the actual API, MCP and schema
  surface.

### Negative

- Two tags per release and a release PR that regenerates snapshots and writes a
  CHANGELOG entry add ceremony to every release.
- The contract classifier must be maintained as FastAPI and FastMCP schema
  output evolves; a false "breaking" result could block a release until fixed.
- Breaking changes must wait for the first release of a month.
- Legacy adoption re-embeds three chunks at startup and depends on the
  embedding service being up; until it is, the index stays `unverified`.
- The indexer normally runs from a source checkout, so `/health` reports no
  product release there. Only the SemVer is always known.

## Out of scope

- Separate versions per Docker image.
- Minor-level feature negotiation between `treeweft-mcp` and the indexer.
- Publishing to PyPI.
- The Postgres migration runner catching and logging migration errors while
  startup continues. It conflicts with the constitution's fail-loud principle
  and is to be filed as its own issue.

## References

- `docs/docker-images.md`: today's CalVer versioning and release procedure.
- `.github/workflows/docker-publish.yml`: the `check-tag` job.
- `src/treeweft/application/indexer_service.py`: `GET /health`.
- `src/treeweft/application/mcp_server.py`: tool implementations and error
  handling.
- `src/treeweft/adapters/milvus/vector_store.py`: `init_collection` and the
  collection schema.
- `src/treeweft/adapters/lancedb/vector_store.py`: the existing dimension check.
- `src/treeweft/adapters/neo4j/graph_store.py` and
  `src/treeweft/adapters/sqlite/graph_store.py`: graph schemas.
- `src/treeweft/__init__.py`: the `TREELOOM_*` environment aliases (issue #14).
- [ADR-003](adr-003-prompt-versioning.md): summary prompts are versioned
  separately and never force a re-index.
