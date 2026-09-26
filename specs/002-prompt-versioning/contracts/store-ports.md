# Store and module functions

## Vector store (behind `treeweft.retriever`, all three backends)

Each function is exported for every `VECTOR_STORE` value. `tests/unit/test_store_shim_exports.py`
checks this.

| Function | Returns | Milvus | LanceDB | ChromaDB |
|---|---|---|---|---|
| `summary_vectors_supported()` | `bool` | `True` | `True` | `False` |
| `async snapshot_source_row_ids(source_id)` | `list[int \| str]` | `query_iterator` over `source_id == "<escaped>"`, `output_fields=["id"]`, `consistency_level="Strong"` | `where("source_id = '<esc>'")`, select `id` | `[]` |
| `async fetch_rows(ids)` | `list[dict]`, full rows | `query(ids=…, output_fields=["*"], consistency_level="Strong")` | `where("id IN (…)")`, all columns | `[]` |
| `async write_summary_vectors(rows, vectors)` | `None` | `upsert` of each full row minus `sparse_vector`, with `summary_vector = v or [0.0]*dim` | `merge_insert("id").when_matched_update_all()` with `summary_vector = v` (None stays NULL) | raises `NotImplementedError` |
| `async count_source_rows(source_id)` | `int` | `query(filter, output_fields=["count(*)"], consistency_level="Strong")` | `count_rows(predicate)` | `0` |

- `vectors` is aligned with `rows`. `None` means no summary; the store applies its own
  no-summary value (research R7).
- The Milvus functions are `MilvusAdapter` methods with module wrappers, so tests can target a
  per-run collection.
- `snapshot_source_row_ids` streams in batches of 10,000 and collects IDs only. For a source with
  about 1M chunks, that is about 8 MB of integers.

## Summary path (`adapters/llm_api/llm_adapter.py`)

```python
SummaryOutcome = tuple[str | None, Literal["cached", "generated", "rejected", "error"]]

async def summarize_with_cache(chunk_text, language, file_path, *, version: int) -> SummaryOutcome
async def cache_get_many(sha1s, prompt_version: int, include_rejected: bool = False) -> dict[str, str]
async def cache_put(sha1, summary, *, prompt_version: int) -> None
async def generate_hyde(query, language=None) -> str | None   # version from prompt_pins.effective("hyde")
```

`application/indexer_runners.py`:

```python
async def _summaries_for_chunks(chunks, *, version: int) -> list[SummaryOutcome]
```

## Pins (`application/prompt_pins.py`)

```python
def effective(operation: str, source_id: str | None = None) -> int
def view() -> PinView
async def load_and_seed() -> None           # startup; raises RuntimeError on a missing table or an unknown pin
async def start_sync() -> None              # LISTEN + reload on (re)connect + periodic reload
async def stop_sync() -> None
async def set_pin(op, scope, version, *, updated_by, dry_run) -> PinChangeResult
async def clear_override(source_id, *, updated_by, dry_run) -> PinChangeResult
```

## Refresh orchestration (`application/prompt_refresh.py`)

```python
async def plan_refreshes(source_ids, target_for) -> RefreshPlan   # enqueue / defer / refuse; pure apart from reads
async def enqueue_refreshes(plan, *, created_by) -> RefreshPlan   # creates jobs and a group
async def enqueue_if_stale(source_id) -> str | None               # post-job hook (R4); never after a resummarize job
```

## Postgres adapters

- `adapters/postgresql/prompt_pin_store.py`, which has these methods:
  - `list_all()`;
  - `upsert(op, scope, version, updated_by)`, which sends `NOTIFY` in the same transaction;
  - `delete(op, scope)`, which also sends `NOTIFY`;
  - `seed_if_absent(op, version)`, an `INSERT … ON CONFLICT DO NOTHING`;
  - `delete_overrides_for_source(source_id)`.
- `PostgreSourceRepository` gains:
  - `mark_summary_refresh(source_id, target)`;
  - `record_summary_version(source_id, version)`, which sets the version and clears the target;
  - `summary_version_histogram()`, which feeds the seeding rule.
