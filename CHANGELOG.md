# Changelog

Each release is headed `## <SemVer> — <CalVer>`: the source version and the
product (image) release it shipped as. See `docs/docker-images.md`,
"Versioning". Sections: Breaking, Requires re-index, Operator configuration,
Added, Fixed.

## Unreleased

### Added

- The index schema stamp (ADR-004 §3): every vector store (Milvus,
  LanceDB, ChromaDB) and graph store (Neo4j, SQLite) records what built
  its data — the schema integer, the embedding model and the vector
  dimension. `GET /health` gains `index_schema` and `index_status` (`ok`,
  `unverified`, `reindex_required` or `rebuilding`, with `reindex_reason`
  and `rebuild_progress` as they apply). While the index is
  `reindex_required`, the search, hydrate-chunks, find-\* and
  graph-explore endpoints, and any index job, are refused with a 409
  naming the mismatch; `treeweft-mcp` passes that reason to the agent
  instead of reporting the indexer as unreachable.
- `POST /index/rebuild` (admin only, `?dry_run=true` to preview): drops
  and recreates the vector and graph stores at the current schema and
  re-indexes every registered source as one job group. Progress is
  visible in `GET /health` and `GET /job-groups/{id}`.
- `INDEX_VERIFY_INTERVAL_SECONDS`, `INDEX_VERIFY_TIMEOUT_SECONDS` and
  `INDEX_STATUS_REFRESH_SECONDS` settings (see `.env.example`).
- `contracts/index_schema.json`: a committed snapshot of the index
  schema, checked by CI so a schema-changing PR must bump
  `INDEX_SCHEMA_VERSION` and the SemVer major.
- The prompt registry (`adapters/llm_api/prompts.py`) and admin-controlled
  pins (ADR-003): `GET /prompt-versions`, and `/prompt-pins/*` endpoints to
  set the deployment pin and per-source chunk-summary overrides, each
  accepting `?dry_run=true` to preview the sources and chunk counts a
  change would refresh before writing anything. `POST
  /sources/{source_id}/resummarize` requests a manual refresh.
- A `resummarize` job kind that refreshes one source's chunk-summary
  vectors and their embeddings to a new prompt version without
  re-indexing; two or more refreshes triggered by one pin change share a
  `prompt-refresh` job group.
- A **Prompts** page next to Backends: per-operation registered versions,
  notes and the deployment pin, and a sources table with built version,
  target version, a stale badge, an override control and a refresh
  action.
- `GET /sources` gains `summary_prompt_version`, `summary_refresh_target`
  and `summary_stale`.

### Operator configuration

- The first start after upgrading verifies an existing (pre-1.1.0) index
  by re-embedding up to 3 sampled chunks against the configured embedding
  model; this needs the embedding service reachable. Until it succeeds,
  the index reports `unverified`: search keeps working, but new index
  jobs are refused. See `docs/upgrading.md`.
- Migration 021 adds `prompt_pins` and the `source_records` columns
  `summary_prompt_version`/`summary_refresh_target`. Prompt pins are
  seeded on first start: an existing deployment's sources were built with
  chunk_summary v3 and hyde v1, so it seeds there; a fresh install seeds
  the latest registered version of each. Nothing changes until an admin
  moves a pin. `PROMPT_PINS_REFRESH_SECONDS` (default 5) controls how
  often each indexer process reloads the pins. Startup fails, naming the
  pin and the versions this build registers, if a stored pin names a
  version it does not know (for example after a downgrade).

## 1.0.0 — 2026.9.24

### Breaking

- `treeweft-mcp` 1.x refuses an indexer older than 1.0.0; after pulling,
  restart or upgrade the indexer too.

### Added

- The source is versioned with SemVer, starting at 1.0.0. Published images keep CalVer.
- `GET /health` (indexer and MCP HTTP server) reports `version` (SemVer) and
  `release` (CalVer, `null` from a source checkout).
- `treeweft-mcp` checks the indexer's major version on first use (cached for
  5 minutes) and returns a clear "Incompatible indexer" error on a mismatch.
- Contract snapshots (`contracts/`) and a unit test that enforce SemVer bumps.
- Project constitution (`.specify/memory/constitution.md`) and the Spec Kit
  workflow (`.specify/`, `.claude/skills/speckit-*`).
- Design records ADR-003 (prompt versioning, proposed) and ADR-004
  (compatibility versioning).

### Fixed

- `treeweft-mcp` no longer reports HTTP error responses from the indexer as
  "Indexer unreachable"; it reports the status code and the indexer's detail.
- The horizontal logo is centred (SVG viewBox cropped to the artwork).
