"""ChromaDB adapter implementing VectorStorePort."""

import uuid
from typing import Any

try:
    import chromadb
    _IMPORT_ERROR: ImportError | None = None
except ImportError as _exc:
    chromadb = None  # type: ignore[assignment]
    _IMPORT_ERROR = _exc

from treeweft.domain.indexing import Chunk, VectorStorePort
from treeweft.domain.index_stamp import IndexStamp, StoreObservation, parse_stamp


class ChromaAdapter(VectorStorePort):
    """ChromaDB adapter for vector storage and search.

    Uses chromadb.PersistentClient for local persistence.
    Results are normalized to the same format as the Milvus adapter.
    """

    COLLECTION_NAME = "treeweft_chunks"

    def __init__(self, path: str):
        if _IMPORT_ERROR is not None:
            raise ImportError(
                "ChromaDB backend selected but 'chromadb' is not installed. "
                "Install with: pip install -e '.[chromadb]'"
            ) from _IMPORT_ERROR
        self.path = path
        self.collection_name = self.COLLECTION_NAME
        self._client = chromadb.PersistentClient(path=path)
        self._collection = None

    def init_collection(self):
        """Create or get the ChromaDB collection."""
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
        )

    def insert(self, chunks: list[Chunk], embeddings: list[list[float]]):
        """Insert chunks with embeddings into the collection."""
        if self._collection is None:
            raise RuntimeError("Collection not initialized. Call init_collection() first.")

        ids = [str(uuid.uuid4()) for _ in chunks]
        metadatas = [
            {
                "file_path": c.file_path,
                "language": c.language,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "source_id": c.source_id,
            }
            for c in chunks
        ]
        documents = [c.text for c in chunks]

        self._collection.add(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents,
        )

    def search(
        self,
        query_embedding: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Query by embedding, returning Milvus-compatible results."""
        if self._collection is None:
            raise RuntimeError("Collection not initialized. Call init_collection() first.")

        kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": top_k,
            "include": ["metadatas", "documents", "distances"],
        }
        if where:
            kwargs["where"] = where

        result = self._collection.query(**kwargs)

        return self._normalize_results(result)

    def hybrid_search(
        self,
        query: str,
        query_embedding: list[float],
        top_k: int,
        where: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Embedding-only search with optional metadata filtering.

        ChromaDB does not support BM25 natively, so hybrid search performs
        an embedding search with optional metadata filter support.
        """
        if self._collection is None:
            raise RuntimeError("Collection not initialized. Call init_collection() first.")

        kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": top_k,
            "include": ["metadatas", "documents", "distances"],
        }
        if where:
            kwargs["where"] = where

        result = self._collection.query(**kwargs)

        return self._normalize_results(result)

    def delete_by_filter(self, field: str, value: str):
        """Delete documents matching a metadata field filter."""
        if self._collection is None:
            raise RuntimeError("Collection not initialized. Call init_collection() first.")

        self._collection.delete(where={field: value})

    def _normalize_results(self, chroma_result: dict) -> list[dict[str, Any]]:
        """Normalize ChromaDB query results to Milvus-compatible format.

        ChromaDB returns:
            {
                "ids": [["id1", "id2"]],
                "distances": [[0.08, 0.15]],
                "metadatas": [[{...}, {...}]],
                "documents": [["text1", "text2"]],
            }

        Normalized format:
            [{
                "id": "id1",
                "distance": 0.926,  # converted to similarity score
                "entity": {
                    "chunk_text": "text1",
                    "file_path": "...",
                    "language": "...",
                    "start_line": 10,
                    "end_line": 25,
                    "source_id": "src-1",
                },
            }]
        """
        ids = chroma_result.get("ids", [[]])[0]
        distances = chroma_result.get("distances", [[]])[0]
        metadatas = chroma_result.get("metadatas", [[]])[0]
        documents = chroma_result.get("documents", [[]])[0]

        if not ids:
            return []

        results = []
        for i, chunk_id in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""
            raw_distance = distances[i] if i < len(distances) else 0.0

            # Convert ChromaDB distance (lower=better) to similarity score (higher=better)
            score = 1.0 / (1.0 + raw_distance)

            results.append({
                "id": chunk_id,
                "distance": score,
                "entity": {
                    "chunk_text": doc,
                    "file_path": meta.get("file_path", ""),
                    "language": meta.get("language", ""),
                    "start_line": meta.get("start_line", 0),
                    "end_line": meta.get("end_line", 0),
                    "source_id": meta.get("source_id", ""),
                },
            })

        return results


# ── Module-level wrappers (mirror milvus/vector_store.py API) ──────

import os

from treeweft.config import require_env
from treeweft.versions import INDEX_SCHEMA_VERSION

CHROMA_PATH = os.environ.get("CHROMA_PATH", "./chroma_data")
VECTOR_DIM = int(require_env("VECTOR_DIM"))
EMBEDDING_MODEL = require_env("EMBEDDING_MODEL")
_adapter: ChromaAdapter | None = None

# ADR-004 §3 index stamp: collection metadata keys. `collection.modify()`
# replaces the whole metadata dict rather than merging into it, so every
# writer here reads the existing metadata first and merges locally.
_STAMP_SCHEMA_KEY = "treeweft.index_schema"
_STAMP_MODEL_KEY = "treeweft.embedding_model"
_STAMP_DIM_KEY = "treeweft.vector_dim"


def _get_adapter() -> ChromaAdapter:
    global _adapter
    if _adapter is None:
        _adapter = ChromaAdapter(path=CHROMA_PATH)
    return _adapter


def _stamp_metadata(stamp: IndexStamp) -> dict[str, str]:
    return {
        _STAMP_SCHEMA_KEY: str(stamp.schema),
        _STAMP_MODEL_KEY: stamp.embedding_model,
        _STAMP_DIM_KEY: str(stamp.vector_dim),
    }


def _stamp_from_metadata(metadata: dict | None):
    if not metadata or _STAMP_SCHEMA_KEY not in metadata:
        return None
    raw = {
        "index_schema": metadata.get(_STAMP_SCHEMA_KEY),
        "embedding_model": metadata.get(_STAMP_MODEL_KEY),
        "vector_dim": metadata.get(_STAMP_DIM_KEY),
    }
    return parse_stamp(raw)  # IndexStamp, or UnreadableStamp if malformed


async def init_collection():
    adapter = _get_adapter()
    is_new = False
    try:
        adapter._client.get_collection(name=adapter.collection_name)
    except Exception:
        is_new = True
    adapter.init_collection()
    if is_new:
        stamp = IndexStamp(schema=INDEX_SCHEMA_VERSION, embedding_model=EMBEDDING_MODEL, vector_dim=VECTOR_DIM)
        adapter._collection.modify(metadata=_stamp_metadata(stamp))


async def insert_chunks(
    chunks: list[dict],
    embeddings: list[list[float]],
    summary_embeddings: list[list[float] | None] | None = None,
):
    adapter = _get_adapter()
    from treeweft.domain.indexing import Chunk
    chunk_objs = [
        Chunk(
            text=c["text"],
            file_path=c.get("file_path", ""),
            language=c.get("language", ""),
            start_line=c.get("start_line", 0),
            end_line=c.get("end_line", 0),
            source_id=c.get("source_id", ""),
        )
        for c in chunks
    ]
    return adapter.insert(chunk_objs, embeddings)


def delete_chunks_by_source(source_id: str):
    adapter = _get_adapter()
    if adapter._collection is None:
        adapter.init_collection()
    try:
        results = adapter._collection.get(where={"source_id": source_id}, include=[])
        ids = results.get("ids", [])
        if ids:
            adapter._collection.delete(ids=ids)
            return len(ids)
    except Exception:
        pass
    return 0


def search(
    query_embedding: list[float],
    top_k: int = 20,
    language: str | None = None,
    path_prefix: str | None = None,
    source_id: str | None = None,
    exclude_source_ids: list[str] | None = None,
) -> list[dict]:
    adapter = _get_adapter()
    # Chroma's local backend can express equality metadata filters (source_id,
    # language) but NOT the path_prefix LIKE or the NOT IN exclusion that a
    # shared-index (all_access) authorization query needs. Fail closed rather
    # than silently return cross-tier results.
    if exclude_source_ids:
        raise RuntimeError(
            "VECTOR_STORE=chromadb cannot enforce per-source authorization "
            "exclusions for shared-index search. Use milvus or lancedb for "
            "multi-tier deployments (see docs/simple-mode.md)."
        )
    where: dict[str, Any] = {}
    if source_id:
        where["source_id"] = source_id
    if language:
        where["language"] = language
    # Chroma needs an $and wrapper for multiple equality clauses.
    chroma_where: dict[str, Any] | None
    if len(where) > 1:
        chroma_where = {"$and": [{k: v} for k, v in where.items()]}
    elif where:
        chroma_where = where
    else:
        chroma_where = None
    return adapter.search(query_embedding, top_k=top_k, where=chroma_where)


# ── Index stamp (ADR-004 §3) ─────────────────────────────────────────────────

async def observe_index() -> StoreObservation:
    adapter = _get_adapter()
    try:
        collection = adapter._client.get_collection(name=adapter.collection_name)
    except chromadb.errors.NotFoundError:
        return StoreObservation(store="vector", backend="chromadb", exists=False, has_data=False, stamp=None)
    except Exception as exc:  # noqa: BLE001 — any other failure means "unreachable"
        return StoreObservation(store="vector", backend="chromadb", exists=True, has_data=False, stamp=None, unreachable=str(exc))
    try:
        has_data = collection.count() > 0
    except Exception as exc:  # noqa: BLE001
        return StoreObservation(store="vector", backend="chromadb", exists=True, has_data=False, stamp=None, unreachable=str(exc))
    return StoreObservation(
        store="vector", backend="chromadb", exists=True, has_data=has_data,
        stamp=_stamp_from_metadata(collection.metadata),
    )


async def write_stamp(stamp: IndexStamp) -> None:
    adapter = _get_adapter()
    if adapter._collection is None:
        adapter.init_collection()
    merged = dict(adapter._collection.metadata or {})
    merged.update(_stamp_metadata(stamp))
    adapter._collection.modify(metadata=merged)


async def sample_chunks(n: int, scan_limit: int = 20) -> list[tuple[str, list[float]]]:
    """Up to `n` (text, vector) pairs from rows shorter than 50 000 characters,
    scanning at most `scan_limit` rows (research R3)."""
    adapter = _get_adapter()
    if adapter._collection is None:
        adapter.init_collection()
    result = adapter._collection.get(limit=scan_limit, include=["documents", "embeddings"])
    documents = result.get("documents") or []
    embeddings = result.get("embeddings")
    embeddings = [] if embeddings is None else list(embeddings)
    out: list[tuple[str, list[float]]] = []
    for text, vec in zip(documents, embeddings):
        text = text or ""
        if len(text) >= 50000:
            continue
        out.append((text, list(vec)))
        if len(out) >= n:
            break
    return out


async def drop_index() -> None:
    adapter = _get_adapter()
    try:
        adapter._client.delete_collection(name=adapter.collection_name)
    except chromadb.errors.NotFoundError:
        pass
    adapter._collection = None


def summary_vectors_supported() -> bool:
    """ChromaDB has no summary vector: insert_chunks drops it (research R7)."""
    return False


async def snapshot_source_row_ids(source_id: str) -> list:
    """No summary vectors on chromadb (research R7): nothing to snapshot."""
    return []


async def fetch_rows(ids: list) -> list[dict]:
    return []


async def write_summary_vectors(rows: list[dict], vectors: list) -> None:
    raise NotImplementedError("chromadb stores no summary vectors")


async def count_source_rows(source_id: str) -> int:
    return 0
