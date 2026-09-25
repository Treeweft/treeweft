# Changelog

Each release is headed `## <SemVer> — <CalVer>`: the source version and the
product (image) release it shipped as. See `docs/docker-images.md`,
"Versioning". Sections: Breaking, Requires re-index, Operator configuration,
Added, Fixed.

## Unreleased

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
