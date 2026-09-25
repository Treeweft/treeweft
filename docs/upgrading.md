# Upgrading

## Which version am I running?

`GET /health` on the indexer (default `http://localhost:8001/health`):

```json
{"status": "ok", "database": "connected", "auth_enabled": false,
 "version": "1.1.0", "release": "2026.10.1",
 "index_schema": 1, "index_status": "ok"}
```

- `version` is the source SemVer. `treeweft-mcp` must have the same MAJOR.
- `release` is the product (image) release, or `null` when the indexer runs
  from a source checkout.
- `index_schema` and `index_status` are the index-schema stamp (ADR-004 §3),
  described below. `status` always stays `"ok"`, in every index state, so
  container health checks don't flap during a rebuild — check `index_status`
  for the index itself.

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

## The index schema stamp (ADR-004 §3)

Every vector store (Milvus, LanceDB, ChromaDB) and graph store (Neo4j,
SQLite) is stamped with what built its data: the schema integer, the
configured embedding model and the vector dimension. At startup, and every
`INDEX_STATUS_REFRESH_SECONDS` (default 5) after, the indexer compares each
store's stamp with its own configuration and reports the result as
`index_status` in `GET /health`:

| `index_status` | Meaning | Search | Index jobs |
|---|---|---|---|
| `ok` | Stamps match, or the store was just created | Yes | Yes |
| `unverified` | Existing (pre-1.1.0) data whose model couldn't yet be verified — usually because the embedding service wasn't reachable at startup | Yes | Refused, with `reindex_reason` explaining why. Retried automatically before the next index job and every `INDEX_VERIFY_INTERVAL_SECONDS` (default 60); no restart needed once the embedding service comes up |
| `reindex_required` | A stored stamp doesn't match the configuration (a changed embedding model, dimension, or schema), or a stamp couldn't be parsed | Refused with a 409 naming the mismatched store and field (`reindex_reason`) | Refused |
| `rebuilding` | An admin rebuild is in progress | Yes once the stores are recreated (409 "retry shortly" while they're still being dropped) | Yes, for the rebuild's own jobs |

A 409 from a read or write route carries `detail`, `reason`, `index_status`
and a `rebuild` pointer; `treeweft-mcp` forwards `detail` to the agent
instead of reporting the indexer as unreachable.

**First start after upgrading a pre-1.1.0 deployment.** The indexer adopts
its existing index automatically: it checks the stored vector dimension,
then re-embeds up to 3 sampled chunks and compares them with their stored
vectors (cosine similarity ≥ 0.99 counts as the same model). If this passes,
every store is stamped and `index_status` becomes `ok` — no operator action,
no re-index. If the embedding service isn't reachable yet, the index reports
`unverified` until it is (see the table above). If the comparison genuinely
fails, the index reports `reindex_required`.

**Known limit:** with `EMBEDDING_PROVIDER=tei`, the indexer does not send
`EMBEDDING_MODEL` to the TEI server — TEI serves whatever model it was
started with. So swapping TEI to a different model under the same
`EMBEDDING_MODEL` name, once the index is already stamped, is not detected.
Restart the indexer (or run a rebuild) after such a change.

### Rebuilding

If `index_status` is `reindex_required`, an admin can rebuild the index:

```bash
# 1. Preview: lists every source and its chunk count, changes nothing.
curl -X POST 'http://localhost:8001/index/rebuild?dry_run=true' -H "Authorization: Bearer $TOKEN"

# 2. Run it: drops and recreates the vector and graph stores at the current
#    schema, stamps them, and re-indexes every registered source as one job
#    group.
curl -X POST 'http://localhost:8001/index/rebuild' -H "Authorization: Bearer $TOKEN"
```

Watch progress in `GET /health` (`rebuild_progress: {done, total}`) or
`GET /job-groups/{group_id}`. Search keeps working throughout, returning
results only from sources already rebuilt. The summary cache (keyed by LLM
model) is kept, so a rebuild triggered by an embedding-model change reuses
cached summaries and only re-embeds. `source_records`, users, groups, keys
and job history are all kept — only the vector and graph data are dropped.

A rebuild refuses to start (409) while any index job is queued or running,
while a community build (`POST /build-community`) is in progress, or while
another rebuild is already running. An interrupted rebuild (the indexer
restarted mid-run) reports `reindex_required` — "a rebuild was interrupted;
run it again" — and is not itself a blocker to running another one.

## Reading the CHANGELOG before upgrading

Check `CHANGELOG.md` between your version and the target: "Breaking" lists
contract changes that need matching client upgrades, "Requires re-index"
means the index schema changed (see above), and "Operator configuration"
lists settings you must change.
