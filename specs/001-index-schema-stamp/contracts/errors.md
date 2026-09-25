# Contract: 409 body for index-state refusals

```json
{"detail": "Index requires rebuild: vector store (milvus): embedding_model is BAAI/bge-m3, configured Qwen/Qwen3-Embedding-0.6B. Run POST /index/rebuild?dry_run=true, then POST /index/rebuild (docs/upgrading.md).",
 "reason": "vector store (milvus): embedding_model is BAAI/bge-m3, configured Qwen/Qwen3-Embedding-0.6B",
 "index_status": "reindex_required",
 "rebuild": "/index/rebuild"}
```

Rules:

- `detail` is a string of at most 300 characters, and must be readable as is. `treeweft-mcp`
  forwards only `detail`, truncated to 300 characters (`mcp_compat.describe_http_error`). The
  reason inside `detail` is cut to fit so the rebuild pointer is never lost. `reason` carries the
  full text.
- `unverified` variant (index jobs only): `detail` begins
  `"Index unverified: <reason>. Index jobs are refused until the embedding model is verified;
  this retries automatically."`, with `index_status: "unverified"`.
- `preparing` variant (a rebuild is recreating the stores in some indexer process): read routes
  and index jobs get `detail` `"Index rebuild in progress: stores are being recreated. Retry
  shortly; progress is in GET /health."`, with `index_status: "rebuilding"`.
- What an agent sees through `treeweft-mcp`:
  `{"error": "Indexer returned HTTP 409: Index requires rebuild: …"}`. It never sees
  "Indexer unreachable".
