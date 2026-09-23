# Migrating from Treeloom

Treeloom was renamed to **Treeweft**. The code, behavior, and data formats are
unchanged; the names are not. This page lists every rename that can affect an
existing install and how to keep an existing index working.

## Quick path: keep an existing stack's data

Add these to `.env` (values shown are the old defaults; use yours if you
changed them):

```bash
COMPOSE_PROJECT_NAME=treeloom          # reuse the old Docker volumes (see below)
DATABASE_URL=postgresql://treeloom:treeloom_pass@localhost:5432/treeloom
POSTGRES_USER=treeloom
POSTGRES_PASSWORD=treeloom_pass
POSTGRES_DB=treeloom
NEO4J_PASSWORD=treeloom_pass
MILVUS_COLLECTION=treeloom_chunks
# simple mode only (after `mv ~/.treeloom ~/.treeweft`, which carries the
# LanceDB store, SQLite graph, and audit log across in one step):
LANCEDB_TABLE=treeloom_chunks
```

Then rename your `TREELOOM_*` variables to `TREEWEFT_*` (next section). Without
these overrides a Treeweft stack starts on fresh, empty stores and every source
must be re-indexed.

## Environment variables: `TREELOOM_*` → `TREEWEFT_*`

Every `TREELOOM_` variable is now `TREEWEFT_` with the same suffix
(`TREELOOM_PROFILE` → `TREEWEFT_PROFILE`, `TREELOOM_HMAC_SECRET` →
`TREEWEFT_HMAC_SECRET`, …).

**Python processes still honor the old names.** On import, `treeweft` copies each
`TREELOOM_*` variable (from the shell or the repo `.env`) to its `TREEWEFT_*`
name unless the new name is already set to a non-empty value, and prints one
`FutureWarning` listing the old names it found. This fallback will be removed in
a future release.

**Docker Compose and the shell scripts do not.** `docker-compose*.yml`,
`run.sh`, `run-simple.sh`, and the UI container entrypoint interpolate only the
`TREEWEFT_*` names (`TREEWEFT_IMAGE_NAMESPACE`, `TREEWEFT_IMAGE_TAG`,
`TREEWEFT_MCP_API_KEY`, `TREEWEFT_API_BASE`, …), and containers never see the
old names. If you run the stack with Compose, rename the variables in `.env`.

## Default names that changed

| What | Old default | New default | Override |
|---|---|---|---|
| Docker volumes | `treeloom_<vol>` (from the checkout dir name) | `treeweft_<vol>` in a `treeweft/` checkout | `COMPOSE_PROJECT_NAME` |
| Postgres user / db / password | `treeloom` / `treeloom` / `treeloom_pass` | `treeweft` / `treeweft` / `treeweft_pass` | `POSTGRES_*`, `DATABASE_URL` |
| Neo4j password (compose default) | `treeloom_pass` | `treeweft_pass` | `NEO4J_PASSWORD` |
| Milvus / Chroma collection | `treeloom_chunks` | `treeweft_chunks` | `MILVUS_COLLECTION` (Chroma: re-index) |
| LanceDB table / path | `treeloom_chunks`, `~/.treeloom/lancedb` | `treeweft_chunks`, `~/.treeweft/lancedb` | `LANCEDB_TABLE`; move the directory or set an absolute `LANCEDB_PATH` |
| SQLite graph (simple mode) | `~/.treeloom/graph.db` | `~/.treeweft/graph.db` | move the directory or set an absolute `GRAPH_DB_PATH` |
| LLM audit log, caches | `~/.treeloom/` | `~/.treeweft/` | `mv ~/.treeloom ~/.treeweft` |

`~` is not expanded in these path variables, so write absolute paths if you
set them.

Postgres and Neo4j fix their credentials when a volume is first created, so a
reused volume needs the **old** credentials above; changing only the defaults
fails authentication.

## Package, commands, and images

| What | Old | New |
|---|---|---|
| Python package / import | `treeloom` | `treeweft` |
| Stdio MCP command | `treeloom-mcp` | `treeweft-mcp` |
| MCP server name | `treeloom` (tools appear as `mcp__treeloom__*`) | `treeweft` (`mcp__treeweft__*`) |
| Benchmark CLI | `python -m treeloom.benchmark` | `python -m treeweft.benchmark` |
| Docker images | `treeloom/{indexer,mcp-server,ui,qwen3-reranker}` | `treeweft/…` |
| Repository | `github.com/treeloom/treeloom` | `github.com/Treeweft/treeweft` |

Update MCP client configs (`.mcp.json`, Claude Desktop, Cursor, …): change the
command to `treeweft-mcp`, rename the server entry, and rename
`TREELOOM_MCP_TOKEN` to `TREEWEFT_MCP_TOKEN` in its `env` block. Any allow-lists
or prompts that name `mcp__treeloom__*` tools need the new prefix.

## Observability and sessions

- **Prometheus metrics** are now `treeweft_*` (was `treeloom_*`). The bundled
  Grafana dashboards (`assets/grafana/`) and alert rules
  (`assets/prometheus/`) are updated; external dashboards or alerts need the
  same rename, and history under the old metric names does not carry over.
- **Trace service names** are `treeweft-indexer` and `treeweft-mcp`.
- **UI session cookie** is `treeweft_session`, so everyone signed in to the
  operator UI has to sign in again once after upgrading. The UI's runtime
  config global is `window.__TREEWEFT_API_BASE__`.

## Benchmark results

Agentic arms are now `treeweft`, `treeweft-facet`, …. `benchmark rollup` reads
pre-rename `_summary.json` files (arm `treeloom`, keys `treeloom_*`) as their
`treeweft` equivalents, so old and new runs aggregate together.
