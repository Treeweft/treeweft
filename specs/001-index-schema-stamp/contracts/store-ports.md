# Contract: Store functions added behind the root shims

The indexer reaches stores only through `treeweft/retriever.py` (vector) and
`treeweft/graph_store.py` (graph). Each new name MUST be implemented by every backend and added to
the shim's export list. For the graph store that list is `_EXPORTED`. A unit test asserts every
backend module defines every exported name.

## Vector store (`treeweft.retriever`): milvus, lancedb, chromadb

| Function | Returns | Notes |
|---|---|---|
| `async observe_index() -> StoreObservation` | see data-model | Never raises for an unreachable store; it fills `unreachable` instead. |
| `async write_stamp(stamp: IndexStamp) -> None` | | Merges with existing metadata/properties; never drops unrelated keys. |
| `async sample_chunks(n: int, scan_limit: int = 20) -> list[tuple[str, list[float]]]` | `(chunk_text, vector)` | Only rows with `len(chunk_text) < 50000`, drawn from at most `scan_limit` rows. The guard retries with `scan_limit=200` before failing "no verifiable chunks". |
| `async drop_index() -> None` | | Drops the collection or table. The next `init_collection()` recreates and stamps it. |
| `async chunk_count(source_id: str) -> int` | | Optional helper. The dry run uses `source_records.chunk_count` as its source of truth. |

`init_collection()` (existing) MUST stamp a collection it creates, with the current configured
stamp.

**Naming relative to ADR-004 §3**: the ADR says both ports gain `read_stamp()` and
`write_stamp()`. On the vector side, `read_stamp()` is part of `observe_index()`, because the
check needs the stamp together with `exists`, `has_data` and the schema's dimension. It is
exposed as `observe_index().stamp`. The graph side keeps a standalone `read_stamp()` as well,
because it has no schema dimension to combine.

## Graph store (`treeweft.graph_store`): neo4j, sqlite

| Function | Returns | Notes |
|---|---|---|
| `async observe_index() -> StoreObservation` | | `has_data` = at least one `Entity` (Neo4j `MATCH (e:Entity) RETURN 1 LIMIT 1`, `fetch(1)`) / one row in `entities` |
| `async read_stamp() -> IndexStamp \| None` | | Used by `observe_index` |
| `async write_stamp(stamp: IndexStamp) -> None` | | Upsert (Neo4j `MERGE (m:TreeweftMeta {id: $id}) SET …`; SQLite `INSERT … ON CONFLICT(id) DO UPDATE`) |
| `async clear_index_data() -> None` | | Deletes everything except the meta node or table. Neo4j is batched (research R7). On Neo4j it delegates to the internal `_clear_index_data(scope_prefix=None)`. A non-None prefix adds `AND n.id STARTS WITH $prefix`, for integration tests only (research R12). |

`clear_all()` (existing, used by tests) MUST also spare the meta node, so a stamp survives it.

## Coordination (Postgres): `adapters/postgresql/`

| Function | Notes |
|---|---|
| `maintenance_lock.acquire(mode: "exclusive" \| "shared") -> MaintenanceLockHandle \| None` | Opens a **dedicated** `asyncpg.connect(DATABASE_URL)` connection, not from the pool, and calls `pg_try_advisory_lock` or `pg_try_advisory_lock_shared` on the key `(hashtext('treeweft'), hashtext('index-maintenance'))`. `handle.release()` unlocks and closes the connection. Returns **None only when Postgres is present and another holder conflicts**; the connection is closed in that case. With no `DATABASE_URL` or no pool it returns a **no-op handle** (`handle.coordinated is False`; `release()` does nothing) and logs a WARNING once (research R13). |
| `maintenance_lock.probe() -> "exclusive" \| "shared" \| None` | Reads `pg_locks` through the pool without taking the lock (research R13). Returns None when nothing holds the lock or there is no Postgres. |
| `JobGroupStore.latest_by_kind(kind) -> JobGroup \| None` | The most recent group of that kind. |
| `JobGroupStore.delete(group_id) -> None` | Used only to remove the group of an aborted rebuild (R7 step 3). |
