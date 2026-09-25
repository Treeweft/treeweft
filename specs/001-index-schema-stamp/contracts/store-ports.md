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
| `async sample_chunks(n: int) -> list[tuple[str, list[float]]]` | `(chunk_text, vector)` | Only rows with `len(chunk_text) < 50000`. |
| `async drop_index() -> None` | | Drops the collection or table. The next `init_collection()` recreates and stamps it. |
| `async chunk_count(source_id: str) -> int` | | Optional helper. The dry run uses `source_records.chunk_count` as its source of truth. |

`init_collection()` (existing) MUST stamp a collection it creates, with the current configured
stamp.

## Graph store (`treeweft.graph_store`): neo4j, sqlite

| Function | Returns | Notes |
|---|---|---|
| `async observe_index() -> StoreObservation` | | `has_data` = at least one `Entity` (Neo4j `MATCH (e:Entity) RETURN 1 LIMIT 1`, `fetch(1)`) / one row in `entities` |
| `async read_stamp() -> IndexStamp \| None` | | Used by `observe_index` |
| `async write_stamp(stamp: IndexStamp) -> None` | | Upsert (Neo4j `MERGE (m:TreeweftMeta {id: $id}) SET …`; SQLite `INSERT … ON CONFLICT(id) DO UPDATE`) |
| `async clear_index_data() -> None` | | Deletes everything except the meta node or table. Neo4j is batched (research R7). |

`clear_all()` (existing, used by tests) MUST also spare the meta node, so a stamp survives it.
