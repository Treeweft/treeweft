"""Unit tests for the Milvus/ChromaDB summary-rewrite store functions (T021).

Covers `snapshot_source_row_ids`, `fetch_rows`, `write_summary_vectors` and
`count_source_rows` — the primitives `_run_resummarize_job` (research R10)
uses to rewrite `summary_vector` in place.

Detroit-style: mock only pymilvus.MilvusClient (external), never internal
classes. ADR-003 "Verified Milvus behaviour": a partial upsert is rejected
on Milvus 2.5, so `write_summary_vectors` must send full rows, minus
`sparse_vector` (BM25 regenerates it server-side); all reads must use
`consistency_level="Strong"` because a read right after an upsert at the
default consistency level returned the stale row.
"""
from unittest.mock import MagicMock, patch

import pytest


def _make_adapter(mock_client, vector_dim=4):
    from treeweft.adapters.milvus.vector_store import MilvusAdapter
    adapter = MilvusAdapter(host="localhost", port="19530", vector_dim=vector_dim)
    adapter._client = mock_client
    return adapter


@pytest.fixture
def mock_client():
    with patch("pymilvus.MilvusClient") as MockClient:
        mc = MagicMock()
        MockClient.return_value = mc
        yield mc


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# snapshot_source_row_ids
# ---------------------------------------------------------------------------

class TestSnapshotSourceRowIds:
    async def test_escapes_a_source_id_with_quote_and_backslash(self, mock_client):
        adapter = _make_adapter(mock_client)
        it = MagicMock()
        it.next.side_effect = [[], []]
        mock_client.query_iterator.return_value = it

        payload = 'src-1"\\'
        await adapter.snapshot_source_row_ids(payload)

        kwargs = mock_client.query_iterator.call_args.kwargs
        # The raw source_id must never appear unescaped in the filter string,
        # and backslash is escaped FIRST so it can't swallow the closing quote.
        assert payload not in kwargs["filter"]
        assert kwargs["filter"] == 'source_id == "src-1\\"\\\\"'

    async def test_uses_id_output_field_and_strong_consistency(self, mock_client):
        adapter = _make_adapter(mock_client)
        it = MagicMock()
        it.next.side_effect = [[], []]
        mock_client.query_iterator.return_value = it

        await adapter.snapshot_source_row_ids("src-1")

        kwargs = mock_client.query_iterator.call_args.kwargs
        assert kwargs["output_fields"] == ["id"]
        assert kwargs["consistency_level"] == "Strong"
        assert kwargs["batch_size"] == 10000
        assert kwargs["collection_name"] == adapter.collection_name

    async def test_collects_ids_across_batches_and_closes_iterator(self, mock_client):
        adapter = _make_adapter(mock_client)
        it = MagicMock()
        it.next.side_effect = [
            [{"id": 1}, {"id": 2}],
            [{"id": 3}],
            [],
        ]
        mock_client.query_iterator.return_value = it

        ids = await adapter.snapshot_source_row_ids("src-1")

        assert ids == [1, 2, 3]
        it.close.assert_called_once()

    async def test_closes_iterator_even_on_error(self, mock_client):
        adapter = _make_adapter(mock_client)
        it = MagicMock()
        it.next.side_effect = RuntimeError("boom")
        mock_client.query_iterator.return_value = it

        with pytest.raises(RuntimeError):
            await adapter.snapshot_source_row_ids("src-1")

        it.close.assert_called_once()


# ---------------------------------------------------------------------------
# fetch_rows
# ---------------------------------------------------------------------------

class TestFetchRows:
    async def test_queries_by_ids_with_all_fields_and_strong_consistency(self, mock_client):
        adapter = _make_adapter(mock_client)
        mock_client.query.return_value = [{"id": 1, "chunk_text": "x"}]

        rows = await adapter.fetch_rows([1, 2, 3])

        mock_client.query.assert_called_once_with(
            adapter.collection_name,
            ids=[1, 2, 3],
            output_fields=["*"],
            consistency_level="Strong",
        )
        assert rows == [{"id": 1, "chunk_text": "x"}]

    async def test_empty_ids_short_circuits_without_a_call(self, mock_client):
        adapter = _make_adapter(mock_client)

        rows = await adapter.fetch_rows([])

        assert rows == []
        mock_client.query.assert_not_called()


# ---------------------------------------------------------------------------
# write_summary_vectors
# ---------------------------------------------------------------------------

class TestWriteSummaryVectors:
    async def test_upserted_rows_have_no_sparse_vector(self, mock_client):
        adapter = _make_adapter(mock_client)
        rows = [{
            "id": 1,
            "chunk_text": "def foo(): pass",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "summary_vector": [0.0, 0.0, 0.0, 0.0],
            "sparse_vector": {"indices": [1], "values": [0.5]},
            "file_path": "src/foo.py",
            "language": "python",
            "start_line": 1,
            "end_line": 3,
            "source_id": "src-1",
        }]

        await adapter.write_summary_vectors(rows, [[0.9, 0.9, 0.9, 0.9]])

        mock_client.upsert.assert_called_once()
        args, kwargs = mock_client.upsert.call_args
        sent = args[1] if len(args) > 1 else kwargs["data"]
        assert len(sent) == 1
        assert "sparse_vector" not in sent[0]

    async def test_keeps_a_dynamic_field(self, mock_client):
        adapter = _make_adapter(mock_client)
        rows = [{
            "id": 1,
            "chunk_text": "x",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "summary_vector": [0.0, 0.0, 0.0, 0.0],
            "sparse_vector": {"indices": [], "values": []},
            "file_path": "src/foo.py",
            "language": "python",
            "start_line": 1,
            "end_line": 3,
            "source_id": "src-1",
            "custom_tag": "team-a",
        }]

        await adapter.write_summary_vectors(rows, [[0.9, 0.9, 0.9, 0.9]])

        sent = mock_client.upsert.call_args[0][1]
        assert sent[0]["custom_tag"] == "team-a"

    async def test_none_vector_becomes_zero_vector_of_dim(self, mock_client):
        adapter = _make_adapter(mock_client, vector_dim=4)
        rows = [{
            "id": 1,
            "chunk_text": "x",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "summary_vector": [0.0, 0.0, 0.0, 0.0],
            "sparse_vector": {},
            "file_path": "f.py",
            "language": "python",
            "start_line": 1,
            "end_line": 1,
            "source_id": "src-1",
        }]

        await adapter.write_summary_vectors(rows, [None])

        sent = mock_client.upsert.call_args[0][1]
        assert sent[0]["summary_vector"] == [0.0, 0.0, 0.0, 0.0]

    async def test_batches_upserts_at_100(self, mock_client):
        adapter = _make_adapter(mock_client, vector_dim=4)
        row = {
            "id": 1,
            "chunk_text": "x",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "summary_vector": [0.0, 0.0, 0.0, 0.0],
            "sparse_vector": {},
            "file_path": "f.py",
            "language": "python",
            "start_line": 1,
            "end_line": 1,
            "source_id": "src-1",
        }
        rows = [dict(row) for _ in range(150)]
        vectors = [[0.1, 0.1, 0.1, 0.1]] * 150

        await adapter.write_summary_vectors(rows, vectors)

        assert mock_client.upsert.call_count == 2


# ---------------------------------------------------------------------------
# count_source_rows
# ---------------------------------------------------------------------------

class TestCountSourceRows:
    async def test_uses_strong_consistency_and_count_star(self, mock_client):
        adapter = _make_adapter(mock_client)
        mock_client.query.return_value = [{"count(*)": 42}]

        count = await adapter.count_source_rows("src-1")

        assert count == 42
        kwargs = mock_client.query.call_args.kwargs
        assert kwargs["consistency_level"] == "Strong"
        assert kwargs["output_fields"] == ["count(*)"]
        # The value is bound, not interpolated.
        assert "src-1" not in kwargs["filter"]
        assert kwargs["filter_params"] == {"f_source_id": "src-1"}

    async def test_empty_result_is_zero(self, mock_client):
        adapter = _make_adapter(mock_client)
        mock_client.query.return_value = []

        count = await adapter.count_source_rows("src-1")

        assert count == 0


# ---------------------------------------------------------------------------
# Module-level wrappers delegate to the default adapter
# ---------------------------------------------------------------------------

class TestModuleWrappers:
    async def test_module_functions_exist_and_delegate(self):
        from treeweft.adapters.milvus import vector_store as vs

        fake_adapter = MagicMock()

        async def _snapshot(source_id):
            return [1, 2]

        async def _fetch(ids):
            return [{"id": 1}]

        async def _write(rows, vectors):
            return None

        async def _count(source_id):
            return 7

        fake_adapter.snapshot_source_row_ids = _snapshot
        fake_adapter.fetch_rows = _fetch
        fake_adapter.write_summary_vectors = _write
        fake_adapter.count_source_rows = _count

        with patch.object(vs, "_get_adapter", return_value=fake_adapter):
            assert await vs.snapshot_source_row_ids("s") == [1, 2]
            assert await vs.fetch_rows([1]) == [{"id": 1}]
            assert await vs.write_summary_vectors([], []) is None
            assert await vs.count_source_rows("s") == 7


# ---------------------------------------------------------------------------
# ChromaDB: no summary vectors (research R7)
# ---------------------------------------------------------------------------

class TestChromaSummaryVectorsUnsupported:
    async def test_summary_vectors_supported_is_false(self):
        from treeweft.adapters.chromadb import vector_store as cvs
        assert cvs.summary_vectors_supported() is False

    async def test_snapshot_source_row_ids_is_empty(self):
        from treeweft.adapters.chromadb import vector_store as cvs
        assert await cvs.snapshot_source_row_ids("src-1") == []

    async def test_fetch_rows_is_empty(self):
        from treeweft.adapters.chromadb import vector_store as cvs
        assert await cvs.fetch_rows([1, 2]) == []

    async def test_write_summary_vectors_raises(self):
        from treeweft.adapters.chromadb import vector_store as cvs
        with pytest.raises(NotImplementedError):
            await cvs.write_summary_vectors([], [])

    async def test_count_source_rows_is_zero(self):
        from treeweft.adapters.chromadb import vector_store as cvs
        assert await cvs.count_source_rows("src-1") == 0
