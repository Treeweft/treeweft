"""LanceDB embedded vector store (VECTOR_STORE=lancedb).

Single-directory, pip-installable store for the simple deployment profile —
no server, no credentials. Functional parity with the Milvus adapter where
it matters:

- hybrid search = dense + optional HyDE-dense + optional summary-dense +
  native BM25 FTS, fused client-side with reciprocal-rank fusion (the same
  scheme as Milvus's RRFRanker)
- `summary_vector` is a real nullable column (no Milvus zero-vector filler)
- `path_prefix` filtering is a native SQL LIKE prefilter
- FTS transparently covers rows added after index creation (verified)

Results are Milvus-SHAPED (`{id, distance, entity: {...}}`) so the
store-agnostic orchestration in application/retrieval.py and the ChunkHit
wire contract work unchanged. `distance` is cosine similarity for dense
search and the fused RRF score for hybrid_search — in both cases higher is
better, and it only surfaces to users when the reranker is down.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from pathlib import Path

from treeweft.config import require_env
from treeweft.domain.index_stamp import IndexStamp, StoreObservation, parse_stamp
from treeweft.versions import INDEX_SCHEMA_VERSION

logger = logging.getLogger(__name__)

LANCEDB_PATH = os.environ.get(
    "LANCEDB_PATH", str(Path.home() / ".treeweft" / "lancedb")
)
TABLE_NAME = os.environ.get("LANCEDB_TABLE", "treeweft_chunks")
VECTOR_DIM = int(require_env("VECTOR_DIM"))
EMBEDDING_MODEL = require_env("EMBEDDING_MODEL")
RRF_K = int(os.environ.get("RRF_K", "60"))

MAX_TEXT_LEN = 50000
FTS_COLUMN = "chunk_text"

# ADR-004 §3 index stamp: field metadata on the vector column, the only
# LanceDB mechanism that survives add/delete/FTS/optimize/reopen AND can be
# applied to a table created before the stamp existed (research R1) — a
# table's own top-level schema metadata can be set at create_table() but not
# changed afterwards in lancedb 0.33.
_STAMP_SCHEMA_KEY = "treeweft.index_schema"
_STAMP_MODEL_KEY = "treeweft.embedding_model"

SEARCH_OUTPUT_FIELDS = [
    "id",
    "chunk_text",
    "file_path",
    "language",
    "start_line",
    "end_line",
    "source_id",
]

_db = None
_table = None
_lock = threading.Lock()


def _connect():
    global _db
    if _db is None:
        import lancedb

        Path(LANCEDB_PATH).mkdir(parents=True, exist_ok=True)
        _db = lancedb.connect(LANCEDB_PATH)
    return _db


def _schema(dim: int | None = None):
    import pyarrow as pa

    dim = VECTOR_DIM if dim is None else dim
    return pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
            pa.field(
                "summary_vector", pa.list_(pa.float32(), dim), nullable=True
            ),
            pa.field("chunk_text", pa.string()),
            pa.field("file_path", pa.string()),
            pa.field("language", pa.string()),
            pa.field("start_line", pa.int64()),
            pa.field("end_line", pa.int64()),
            pa.field("source_id", pa.string()),
        ]
    )


def _current_stamp_metadata(tbl) -> dict[str, str]:
    """The vector field's existing metadata as str→str, or {} if none.

    lancedb 0.33 hands metadata back as bytes→bytes but only accepts
    str→str on the way in (`replace_field_metadata`), so every read here
    decodes it for round-tripping through a write.
    """
    raw = dict(tbl.schema.field("vector").metadata or {})
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else v
        out[key] = val
    return out


def _stamp_field_metadata(tbl, stamp: IndexStamp) -> None:
    merged = _current_stamp_metadata(tbl)
    merged[_STAMP_SCHEMA_KEY] = str(stamp.schema)
    merged[_STAMP_MODEL_KEY] = stamp.embedding_model
    tbl.replace_field_metadata("vector", merged)


def _get_table():
    """Open-or-create the table + FTS index. Idempotent, thread-safe."""
    global _table
    if _table is not None:
        return _table
    with _lock:
        if _table is not None:
            return _table
        db = _connect()
        if TABLE_NAME in db.list_tables().tables:
            tbl = db.open_table(TABLE_NAME)
            field = tbl.schema.field("vector")
            existing_dim = field.type.list_size
            if existing_dim != VECTOR_DIM:
                raise RuntimeError(
                    f"LanceDB table {TABLE_NAME!r} has vector dim {existing_dim} "
                    f"but VECTOR_DIM={VECTOR_DIM}. Changing embedding models "
                    f"requires deleting {LANCEDB_PATH} and re-indexing."
                )
        else:
            tbl = db.create_table(TABLE_NAME, schema=_schema())
            _stamp_field_metadata(
                tbl, IndexStamp(schema=INDEX_SCHEMA_VERSION, embedding_model=EMBEDDING_MODEL, vector_dim=VECTOR_DIM)
            )
        has_fts = any(
            idx.columns == ["chunk_text"] for idx in tbl.list_indices()
        )
        if not has_fts:
            # Native (non-tantivy) FTS: BM25, persisted in the table dir, and
            # rows added later are searched without reindexing (spike-verified).
            tbl.create_fts_index("chunk_text", use_tantivy=False)
        _table = tbl
    return _table


def _esc(value: str) -> str:
    return value.replace("'", "''")


def _build_filter(
    language: str | None,
    path_prefix: str | None,
    source_id: str | None,
    exclude_source_ids: list[str] | None = None,
) -> str | None:
    parts: list[str] = []
    if language:
        parts.append(f"language = '{_esc(language)}'")
    if path_prefix:
        parts.append(f"file_path LIKE '{_esc(path_prefix)}%'")
    if source_id:
        parts.append(f"source_id = '{_esc(source_id)}'")
    if exclude_source_ids:
        # Exclude denied sources from a shared-index (all_access) query.
        literal = ", ".join(f"'{_esc(s)}'" for s in exclude_source_ids)
        parts.append(f"source_id NOT IN ({literal})")
    return " AND ".join(parts) or None


def _to_hit(row: dict, score: float) -> dict:
    return {
        "id": row.get("id", ""),
        "distance": score,
        "entity": {
            "chunk_text": row.get("chunk_text", ""),
            "file_path": row.get("file_path", ""),
            "language": row.get("language", ""),
            "start_line": int(row.get("start_line") or 0),
            "end_line": int(row.get("end_line") or 0),
            "source_id": row.get("source_id", ""),
        },
    }


# ---------------------------------------------------------------------------
# Module surface (mirrors adapters/milvus/vector_store.py)
# ---------------------------------------------------------------------------


async def init_collection():
    """Create the table + FTS index if missing. Idempotent."""
    await asyncio.to_thread(_get_table)


async def insert_chunks(
    chunks: list[dict],
    embeddings: list[list[float]],
    summary_embeddings: list[list[float] | None] | None = None,
):
    rows = []
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        if hasattr(chunk, "text"):
            text = chunk.text
            meta = {
                "file_path": chunk.file_path,
                "language": getattr(chunk, "language", "") or "",
                "start_line": int(getattr(chunk, "start_line", 0) or 0),
                "end_line": int(getattr(chunk, "end_line", 0) or 0),
                "source_id": getattr(chunk, "source_id", "") or "",
            }
        else:
            text = chunk["text"]
            meta = {
                "file_path": chunk["file_path"],
                "language": chunk.get("language", "") or "",
                "start_line": int(chunk.get("start_line", 0) or 0),
                "end_line": int(chunk.get("end_line", 0) or 0),
                "source_id": chunk.get("source_id", "") or "",
            }
        summary = None
        if summary_embeddings is not None and summary_embeddings[i] is not None:
            summary = summary_embeddings[i]
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "vector": emb,
                "summary_vector": summary,  # real NULL, not a zero vector
                "chunk_text": text[:MAX_TEXT_LEN],
                **meta,
            }
        )
    if rows:
        tbl = _get_table()
        await asyncio.to_thread(tbl.add, rows)


def _dense_leg(
    tbl, vec: list[float], column: str, limit: int, where: str | None
) -> list[dict]:
    q = tbl.search(vec, vector_column_name=column).distance_type("cosine")
    if where:
        q = q.where(where, prefilter=True)
    return q.select(SEARCH_OUTPUT_FIELDS).limit(limit).to_list()


def _fts_leg(tbl, query_text: str, limit: int, where: str | None) -> list[dict]:
    try:
        q = tbl.search(query_text, query_type="fts")
        if where:
            q = q.where(where, prefilter=True)
        return q.select(SEARCH_OUTPUT_FIELDS).limit(limit).to_list()
    except Exception:
        # Empty index / FTS syntax edge — hybrid degrades to dense-only
        # rather than failing the search.
        logger.warning("lancedb FTS leg failed; hybrid degrades to dense",
                       exc_info=True)
        return []


def search(
    query_embedding: list[float],
    top_k: int = 20,
    language: str | None = None,
    path_prefix: str | None = None,
    source_id: str | None = None,
    exclude_source_ids: list[str] | None = None,
) -> list[dict]:
    """Dense vector search with optional metadata prefilters."""
    tbl = _get_table()
    where = _build_filter(language, path_prefix, source_id, exclude_source_ids)
    rows = _dense_leg(tbl, query_embedding, "vector", top_k, where)
    # _distance is cosine DISTANCE (0 = identical); surface similarity.
    return [_to_hit(r, 1.0 - float(r.get("_distance", 1.0))) for r in rows]


def hybrid_search(
    query_text: str,
    query_dense: list[float],
    query_summary_dense: list[float] | None = None,
    hyde_dense: list[float] | None = None,
    top_k: int = 60,
    language: str | None = None,
    path_prefix: str | None = None,
    source_id: str | None = None,
    rrf_k: int | None = None,
    exclude_source_ids: list[str] | None = None,
) -> list[dict]:
    """Dense + HyDE + summary + BM25 legs fused with reciprocal-rank fusion.

    Same leg set and fusion scheme as the Milvus adapter's hybrid_search
    (RRFRanker): fused(id) = sum over legs of 1/(k + rank_in_leg), 1-based.
    """
    tbl = _get_table()
    where = _build_filter(language, path_prefix, source_id, exclude_source_ids)
    k = rrf_k or RRF_K

    legs: list[list[dict]] = [_dense_leg(tbl, query_dense, "vector", top_k, where)]
    if hyde_dense is not None:
        legs.append(_dense_leg(tbl, hyde_dense, "vector", top_k, where))
    if query_summary_dense is not None:
        legs.append(
            _dense_leg(tbl, query_summary_dense, "summary_vector", top_k, where)
        )
    legs.append(_fts_leg(tbl, query_text, top_k, where))

    fused: dict[str, float] = {}
    rows_by_id: dict[str, dict] = {}
    for leg in legs:
        for rank, row in enumerate(leg, start=1):
            rid = row["id"]
            fused[rid] = fused.get(rid, 0.0) + 1.0 / (k + rank)
            rows_by_id.setdefault(rid, row)

    ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [_to_hit(rows_by_id[rid], score) for rid, score in ranked]


def delete_by_filter(field: str, value: str):
    tbl = _get_table()
    tbl.delete(f"{field} = '{_esc(value)}'")


def delete_chunks_by_source(source_id: str):
    delete_by_filter("source_id", source_id)


def delete_chunks_by_file(source_id: str, file_path: str) -> int:
    tbl = _get_table()
    predicate = (
        f"source_id = '{_esc(source_id)}' AND file_path = '{_esc(file_path)}'"
    )
    count = tbl.count_rows(predicate)
    if count:
        tbl.delete(predicate)
    return count


async def list_indexed_paths(source_id: str) -> set[str]:
    """Distinct file_paths already indexed for a source (job-resume support)."""

    def _scan() -> set[str]:
        tbl = _get_table()
        predicate = f"source_id = '{_esc(source_id)}'"
        total = tbl.count_rows(predicate)
        if not total:
            return set()
        rows = (
            tbl.search(None)
            .where(predicate)
            .select(["file_path"])
            .limit(total)
            .to_list()
        )
        return {r["file_path"] for r in rows if r.get("file_path")}

    return await asyncio.to_thread(_scan)


# ── Index stamp (ADR-004 §3) ─────────────────────────────────────────────────

def _stamp_from_metadata(tbl):
    meta = _current_stamp_metadata(tbl)
    if _STAMP_SCHEMA_KEY not in meta or _STAMP_MODEL_KEY not in meta:
        return None
    raw = {
        "index_schema": meta[_STAMP_SCHEMA_KEY],
        "embedding_model": meta[_STAMP_MODEL_KEY],
        # The dimension is authoritative from the field type itself, never a
        # stored key (data-model.md, IndexStamp "vector_dim" rule).
        "vector_dim": tbl.schema.field("vector").type.list_size,
    }
    return parse_stamp(raw)  # IndexStamp, or UnreadableStamp if malformed


async def observe_index() -> StoreObservation:
    def _observe() -> StoreObservation:
        db = _connect()
        if TABLE_NAME not in db.list_tables().tables:
            return StoreObservation(store="vector", backend="lancedb", exists=False, has_data=False, stamp=None)
        tbl = db.open_table(TABLE_NAME)
        has_data = tbl.count_rows() > 0
        dim = tbl.schema.field("vector").type.list_size
        return StoreObservation(
            store="vector", backend="lancedb", exists=True, has_data=has_data,
            stamp=_stamp_from_metadata(tbl), schema_dim=dim,
        )

    try:
        return await asyncio.to_thread(_observe)
    except Exception as exc:  # noqa: BLE001 — any failure means "unreachable"
        return StoreObservation(store="vector", backend="lancedb", exists=True, has_data=False, stamp=None, unreachable=str(exc))


async def write_stamp(stamp: IndexStamp) -> None:
    def _write():
        tbl = _get_table()
        _stamp_field_metadata(tbl, stamp)

    await asyncio.to_thread(_write)


async def sample_chunks(n: int, scan_limit: int = 20) -> list[tuple[str, list[float]]]:
    """Up to `n` (text, vector) pairs from rows shorter than MAX_TEXT_LEN,
    scanning at most `scan_limit` rows (research R3)."""

    def _sample() -> list[tuple[str, list[float]]]:
        tbl = _get_table()
        total = tbl.count_rows()
        if not total:
            return []
        rows = (
            tbl.search(None)
            .select(["chunk_text", "vector"])
            .limit(min(scan_limit, total))
            .to_list()
        )
        out = []
        for row in rows:
            text = row.get("chunk_text") or ""
            if len(text) >= MAX_TEXT_LEN:
                continue
            out.append((text, list(row["vector"])))
            if len(out) >= n:
                break
        return out

    return await asyncio.to_thread(_sample)


async def drop_index() -> None:
    global _table

    def _drop():
        db = _connect()
        if TABLE_NAME in db.list_tables().tables:
            db.drop_table(TABLE_NAME)

    await asyncio.to_thread(_drop)
    with _lock:
        _table = None


def summary_vectors_supported() -> bool:
    """LanceDB stores a nullable summary_vector (research R7)."""
    return True
