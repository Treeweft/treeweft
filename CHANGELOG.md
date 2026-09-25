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

### Operator configuration

- The first start after upgrading verifies an existing (pre-1.1.0) index
  by re-embedding up to 3 sampled chunks against the configured embedding
  model; this needs the embedding service reachable. Until it succeeds,
  the index reports `unverified`: search keeps working, but new index
  jobs are refused. See `docs/upgrading.md`.

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
