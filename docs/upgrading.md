# Upgrading

## Which version am I running?

`GET /health` on the indexer (default `http://localhost:8001/health`):

```json
{"status": "ok", "database": "connected", "auth_enabled": false,
 "version": "1.0.0", "release": "2026.10.1"}
```

- `version` is the source SemVer. `treeweft-mcp` must have the same MAJOR.
- `release` is the product (image) release, or `null` when the indexer runs
  from a source checkout.

## "Incompatible indexer" from an MCP tool

`treeweft-mcp` and the indexer have different SemVer majors, for example a
`treeweft-mcp` from one checkout talking to an indexer image from another. The
error names both versions. Upgrade whichever side is older so the majors
match. If the error says the indexer "predates version reporting", the
indexer is older than 1.0.0 — restart or upgrade the host indexer (for
example, rerun `./run.sh` after pulling) so both sides are 1.x.

A successful check is cached for 5 minutes, so an indexer that changes major
version after a successful check can go unnoticed for up to 5 minutes. A
mismatch is never cached, so fixing a mismatch takes effect on the next
call.

## Reading the CHANGELOG before upgrading

Check `CHANGELOG.md` between your version and the target: "Breaking" lists
contract changes that need matching client upgrades, and "Operator
configuration" lists settings you must change.
