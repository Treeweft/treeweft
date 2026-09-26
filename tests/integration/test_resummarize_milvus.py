"""Summary-vector rewrite round-trip against a real Milvus (ADR-003 §2,
research R10, T041).

Proves what the `resummarize` job's four store primitives
(`snapshot_source_row_ids` -> `fetch_rows` -> `write_summary_vectors` ->
`count_source_rows`) actually do against a live Milvus 2.5.x, including the
Strong-consistency guarantee the unit tests (mocked store) cannot exercise:
that Milvus's full-row upsert re-keys the primary key, and that a query
immediately after the upsert — with no flush — already sees only the new
ids. Uses a uniquely named collection built directly from
`MilvusAdapter(collection_name=...)`, never the module-level wrappers
(those address the configured MILVUS_COLLECTION).

Run against a standalone instance:

    docker compose --profile local-infra up -d milvus
    MILVUS_TEST_URI=http://localhost:19530 \
      env -u PYTHONPATH python -m pytest tests/integration/test_resummarize_milvus.py -v

Skipped unless MILVUS_TEST_URI is set — no service, no silent pass.
"""
from __future__ import annotations

import os
import uuid

import pytest

URI = os.environ.get("MILVUS_TEST_URI")
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not URI, reason="set MILVUS_TEST_URI to run"),
    pytest.mark.asyncio,
]

COLL = f"itest_resum_{uuid.uuid4().hex[:8]}"
DIM = 4
SRC = "itest-resum-source"

# Near-orthogonal code vectors so COSINE search can distinguish the three
# rows (three collinear vectors, e.g. [0.1]*4 vs [0.2]*4, would tie).
VEC_A = [0.9, 0.1, 0.1, 0.1]
VEC_B = [0.1, 0.9, 0.1, 0.1]
VEC_C = [0.1, 0.1, 0.9, 0.1]

TEXT_A = "def alpha_resum(): return 1"
TEXT_B = "def beta_resum(): return 2"
TEXT_C = "def gamma_resum(): return 3"

# The rewrite targets: A and B get new summary vectors, C's existing
# (nonzero) summary vector is rewritten to None -> the store's zero value.
NEW_SUMMARY_A = [0.4, 0.3, 0.2, 0.1]
NEW_SUMMARY_B = [0.1, 0.2, 0.3, 0.4]
OLD_SUMMARY_C = [0.5, 0.5, 0.5, 0.5]


@pytest.fixture
def adapter():
    from pymilvus import MilvusClient

    from treeweft.adapters.milvus import vector_store as vs

    raw = MilvusClient(uri=URI)
    if raw.has_collection(COLL):
        raw.drop_collection(COLL)

    host, _, port = URI.split("://", 1)[1].partition(":")
    a = vs.MilvusAdapter(host=host, port=port or "19530", collection_name=COLL, vector_dim=DIM)
    a.uri = URI  # the adapter assumes https; the local standalone is plain http
    yield a
    if raw.has_collection(COLL):
        raw.drop_collection(COLL)


def _entity_rows(results) -> list[dict]:
    """Hit -> entity dict, the same shape chunk_hit_from_milvus reads."""
    return [hit["entity"] for hit in results]


class TestSummaryVectorRewrite:
    async def test_rewrite_round_trip(self, adapter):
        from pymilvus import MilvusClient

        mc = adapter._get_client()

        # 1. Seed via the adapter's real insert path (chunks A, B — no
        # summary embeddings, so they start at the store's zero value).
        await adapter.init_collection()
        await adapter.insert(
            [
                {"text": TEXT_A, "file_path": "/resum_a.py", "language": "python",
                 "start_line": 1, "end_line": 1, "source_id": SRC},
                {"text": TEXT_B, "file_path": "/resum_b.py", "language": "python",
                 "start_line": 1, "end_line": 1, "source_id": SRC},
            ],
            [VEC_A, VEC_B],
        )

        # 2. Chunk C: the adapter's insert() only accepts the fixed field
        # set, so a dynamic field (`team`) and a nonzero starting summary
        # vector both require a raw MilvusClient insert — the collection
        # has enable_dynamic_field=True (production schema), so the extra
        # key is stored as a dynamic field.
        mc.insert(COLL, [{
            "chunk_text": TEXT_C,
            "vector": VEC_C,
            "summary_vector": OLD_SUMMARY_C,
            "file_path": "/resum_c.py",
            "language": "python",
            "start_line": 1,
            "end_line": 1,
            "source_id": SRC,
            "team": "itest-team",
        }])
        mc.flush(COLL)
        mc.load_collection(COLL)

        # 3. Snapshot -> fetch, via the adapter (Strong consistency).
        ids_before = await adapter.snapshot_source_row_ids(SRC)
        assert len(ids_before) == 3

        rows = await adapter.fetch_rows(ids_before)
        assert len(rows) == 3
        by_path_before = {r["file_path"]: r for r in rows}
        assert by_path_before["/resum_c.py"]["team"] == "itest-team"

        # 4. Rewrite: A and B get new summary vectors, C's is cleared (None).
        targets = {
            "/resum_a.py": NEW_SUMMARY_A,
            "/resum_b.py": NEW_SUMMARY_B,
            "/resum_c.py": None,
        }
        vectors = [targets[row["file_path"]] for row in rows]
        await adapter.write_summary_vectors(rows, vectors)

        # 5. The load-bearing Strong-consistency check (research R10): with
        # no flush in between, an immediate re-snapshot already sees the
        # upsert's new ids and none of the old ones — Milvus's full-row
        # upsert re-keys the primary key.
        ids_after = await adapter.snapshot_source_row_ids(SRC)
        assert len(ids_after) == 3
        assert set(ids_after).isdisjoint(set(ids_before))

        # 6. Row count unchanged, no duplicate (file_path, start_line).
        count = await adapter.count_source_rows(SRC)
        assert count == 3

        post_rows = mc.query(
            COLL,
            filter="source_id == {f_source_id}",
            filter_params={"f_source_id": SRC},
            output_fields=["*"],
            consistency_level="Strong",
        )
        assert len(post_rows) == 3
        pairs = [(r["file_path"], r["start_line"]) for r in post_rows]
        assert len(pairs) == len(set(pairs))
        by_path_after = {r["file_path"]: r for r in post_rows}

        # 7. New summary_vector values present; None -> zeros.
        def _approx(vec, expected, tol=1e-4):
            assert len(vec) == len(expected)
            assert all(abs(a - b) < tol for a, b in zip(vec, expected))

        _approx(by_path_after["/resum_a.py"]["summary_vector"], NEW_SUMMARY_A)
        _approx(by_path_after["/resum_b.py"]["summary_vector"], NEW_SUMMARY_B)
        _approx(by_path_after["/resum_c.py"]["summary_vector"], [0.0] * DIM)

        # 8. vector, chunk_text, and the dynamic field are unchanged.
        _approx(by_path_after["/resum_a.py"]["vector"], VEC_A)
        _approx(by_path_after["/resum_b.py"]["vector"], VEC_B)
        _approx(by_path_after["/resum_c.py"]["vector"], VEC_C)
        assert by_path_after["/resum_a.py"]["chunk_text"] == TEXT_A
        assert by_path_after["/resum_b.py"]["chunk_text"] == TEXT_B
        assert by_path_after["/resum_c.py"]["chunk_text"] == TEXT_C
        assert by_path_after["/resum_c.py"]["team"] == "itest-team"

        # 9. Dense search on `vector` still returns the chunks — the code
        # vector survived the summary-only rewrite and stays searchable.
        # Flush so the rewritten (re-keyed) rows are in a sealed, indexed
        # segment rather than relying on unindexed growing-segment search.
        mc.flush(COLL)
        dense_hits = adapter.search(query_embedding=VEC_A, top_k=3, source_id=SRC)
        dense_paths = [h["entity"]["file_path"] for h in dense_hits]
        assert "/resum_a.py" in dense_paths
        assert dense_paths[0] == "/resum_a.py"

        # 10. BM25 search on `chunk_text` still returns the chunks (raw
        # MilvusClient search — the adapter's search() is dense-only).
        bm25_results = mc.search(
            COLL,
            data=["alpha_resum"],
            anns_field="sparse_vector",
            search_params={"metric_type": "BM25", "params": {"drop_ratio_search": 0.0}},
            limit=3,
            filter="source_id == {f_source_id}",
            filter_params={"f_source_id": SRC},
            output_fields=["file_path", "chunk_text"],
        )
        bm25_paths = [h["entity"]["file_path"] for h in bm25_results[0]]
        assert "/resum_a.py" in bm25_paths
