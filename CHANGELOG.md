# Changelog

Each release is headed `## <SemVer> — <CalVer>`: the source version and the
product (image) release it shipped as. See `docs/docker-images.md`,
"Versioning". Sections: Breaking, Requires re-index, Operator configuration,
Added, Fixed.

## Unreleased

### Added

- The source is versioned with SemVer, starting at 1.0.0. Published images keep CalVer.
- `GET /health` (indexer and MCP HTTP server) reports `version` (SemVer) and
  `release` (CalVer, `null` from a source checkout).
- `treeweft-mcp` checks the indexer's major version on first use (cached for
  5 minutes) and returns a clear "Incompatible indexer" error on a mismatch.
- Contract snapshots (`contracts/`) and a unit test that enforce SemVer bumps.

### Fixed

- `treeweft-mcp` no longer reports HTTP error responses from the indexer as
  "Indexer unreachable"; it reports the status code and the indexer's detail.
