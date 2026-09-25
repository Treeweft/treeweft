"""Milvus vector store index stamp (ADR-004 §3): collection properties.

Detroit-style: mock only pymilvus.MilvusClient (external), like
test_milvus_adapter.py.
"""
from unittest.mock import MagicMock, patch

import pytest

from treeweft.domain.index_stamp import IndexStamp

STAMP = IndexStamp(schema=1, embedding_model="Qwen/Qwen3-Embedding-0.6B", vector_dim=1024)


def _make_adapter(mock_client, dim=1024):
    from treeweft.adapters.milvus.vector_store import MilvusAdapter
    adapter = MilvusAdapter(host="localhost", port="19530", vector_dim=dim)
    adapter._client = mock_client
    return adapter


@pytest.fixture
def mock_client():
    with patch("pymilvus.MilvusClient") as MockClient:
        mc = MagicMock()
        mc.prepare_index_params.return_value = MagicMock()
        MockClient.return_value = mc
        yield mc


@pytest.fixture(autouse=True)
def _model(monkeypatch):
    from treeweft.adapters.milvus import vector_store as vs
    monkeypatch.setattr(vs, "EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")


pytestmark = pytest.mark.asyncio


class TestInitCollectionStamps:
    async def test_create_collection_gets_properties(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        with patch.object(vs.MilvusClient, "has_collection", MagicMock(return_value=False)), \
             patch.object(vs.MilvusClient, "create_collection", MagicMock()) as create_c:
            await adapter.init_collection()
        _, kwargs = create_c.call_args
        assert kwargs["properties"] == {
            "treeweft.index_schema": "1",
            "treeweft.embedding_model": "Qwen/Qwen3-Embedding-0.6B",
        }

    async def test_uses_build_collection_schema(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        with patch.object(vs.MilvusClient, "has_collection", MagicMock(return_value=False)), \
             patch.object(vs.MilvusClient, "create_collection", MagicMock()) as create_c:
            await adapter.init_collection()
        _, kwargs = create_c.call_args
        assert kwargs["schema"] is not None
        field_names = [f.name for f in kwargs["schema"].fields]
        assert "vector" in field_names
        assert "sparse_vector" in field_names

    async def test_index_params_include_hnsw_and_bm25(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        kinds = {spec["field_name"]: spec["index_type"] for spec in vs.INDEX_PARAMS}
        assert kinds["vector"] == "HNSW"
        assert kinds["sparse_vector"] == "SPARSE_INVERTED_INDEX"


class TestObserveIndex:
    async def test_collection_absent_reports_not_exists(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        with patch.object(vs.MilvusClient, "has_collection", MagicMock(return_value=False)):
            obs = await adapter.observe_index()
        assert obs.exists is False

    async def test_reads_dimension_from_vector_field_params(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        describe = {
            "fields": [
                {"name": "id", "params": {}},
                {"name": "vector", "params": {"dim": 1024}},
            ],
            "properties": {},
        }
        with patch.object(vs.MilvusClient, "has_collection", MagicMock(return_value=True)), \
             patch.object(vs.MilvusClient, "describe_collection", MagicMock(return_value=describe)), \
             patch.object(vs.MilvusClient, "get_collection_stats", MagicMock(return_value={"row_count": 0})):
            obs = await adapter.observe_index()
        assert obs.schema_dim == 1024

    async def test_has_data_from_row_count(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        describe = {"fields": [{"name": "vector", "params": {"dim": 1024}}], "properties": {}}
        with patch.object(vs.MilvusClient, "has_collection", MagicMock(return_value=True)), \
             patch.object(vs.MilvusClient, "describe_collection", MagicMock(return_value=describe)), \
             patch.object(vs.MilvusClient, "get_collection_stats", MagicMock(return_value={"row_count": 42})):
            obs = await adapter.observe_index()
        assert obs.has_data is True

    async def test_stamp_parsed_from_properties_strings(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        describe = {
            "fields": [{"name": "vector", "params": {"dim": 1024}}],
            "properties": {"treeweft.index_schema": "1", "treeweft.embedding_model": "Qwen/Qwen3-Embedding-0.6B"},
        }
        with patch.object(vs.MilvusClient, "has_collection", MagicMock(return_value=True)), \
             patch.object(vs.MilvusClient, "describe_collection", MagicMock(return_value=describe)), \
             patch.object(vs.MilvusClient, "get_collection_stats", MagicMock(return_value={"row_count": 1})):
            obs = await adapter.observe_index()
        assert obs.stamp == STAMP

    async def test_zero_row_count_confirmed_empty_by_query_when_unstamped(self, mock_client):
        """get_collection_stats()['row_count'] only reflects flushed segments
        (confirmed live against Milvus 2.5.4 in tests/integration) — an
        unstamped store reporting 0 must be double-checked with a direct
        query before legacy adoption trusts 'no data'."""
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        describe = {"fields": [{"name": "vector", "params": {"dim": 1024}}], "properties": {}}
        with patch.object(vs.MilvusClient, "has_collection", MagicMock(return_value=True)), \
             patch.object(vs.MilvusClient, "describe_collection", MagicMock(return_value=describe)), \
             patch.object(vs.MilvusClient, "get_collection_stats", MagicMock(return_value={"row_count": 0})), \
             patch.object(vs.MilvusClient, "query", MagicMock(return_value=[])) as query:
            obs = await adapter.observe_index()
        assert obs.has_data is False
        query.assert_called_once()
        _, kwargs = query.call_args
        assert kwargs["limit"] == 1

    async def test_zero_row_count_but_unflushed_rows_found_by_query(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        describe = {"fields": [{"name": "vector", "params": {"dim": 1024}}], "properties": {}}
        with patch.object(vs.MilvusClient, "has_collection", MagicMock(return_value=True)), \
             patch.object(vs.MilvusClient, "describe_collection", MagicMock(return_value=describe)), \
             patch.object(vs.MilvusClient, "get_collection_stats", MagicMock(return_value={"row_count": 0})), \
             patch.object(vs.MilvusClient, "query", MagicMock(return_value=[{"id": 1}])):
            obs = await adapter.observe_index()
        assert obs.has_data is True

    async def test_zero_row_count_with_stamp_skips_confirmation_query(self, mock_client):
        """Once a stamp exists, has_data no longer decides fresh-vs-legacy
        adoption (domain/index_stamp.py decide_store), so the extra query
        would be pure waste."""
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        describe = {
            "fields": [{"name": "vector", "params": {"dim": 1024}}],
            "properties": {"treeweft.index_schema": "1", "treeweft.embedding_model": "Qwen/Qwen3-Embedding-0.6B"},
        }
        with patch.object(vs.MilvusClient, "has_collection", MagicMock(return_value=True)), \
             patch.object(vs.MilvusClient, "describe_collection", MagicMock(return_value=describe)), \
             patch.object(vs.MilvusClient, "get_collection_stats", MagicMock(return_value={"row_count": 0})), \
             patch.object(vs.MilvusClient, "query", MagicMock(return_value=[{"id": 1}])) as query:
            obs = await adapter.observe_index()
        assert obs.has_data is False
        query.assert_not_called()

    async def test_client_error_sets_unreachable_and_does_not_raise(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        with patch.object(vs.MilvusClient, "has_collection", MagicMock(side_effect=RuntimeError("connection refused"))):
            obs = await adapter.observe_index()
        assert obs.unreachable is not None
        assert "connection refused" in obs.unreachable


class TestWriteStamp:
    async def test_calls_alter_collection_properties(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        with patch.object(vs.MilvusClient, "alter_collection_properties", MagicMock()) as alter:
            await adapter.write_stamp(STAMP)
        args, _ = alter.call_args
        assert args[1] == adapter.collection_name
        assert args[2] == {"treeweft.index_schema": "1", "treeweft.embedding_model": "Qwen/Qwen3-Embedding-0.6B"}


class TestSampleChunks:
    async def test_queries_with_empty_filter_and_limit(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client, dim=4)
        rows = [{"chunk_text": "short", "vector": [0.1, 0.2, 0.3, 0.4]}]
        with patch.object(vs.MilvusClient, "query", MagicMock(return_value=rows)) as query:
            samples = await adapter.sample_chunks(3)
        args, kwargs = query.call_args
        assert kwargs["filter"] == ""
        assert kwargs["output_fields"] == ["chunk_text", "vector"]
        assert kwargs["limit"] == 20
        assert samples == [("short", [0.1, 0.2, 0.3, 0.4])]

    async def test_skips_long_rows(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client, dim=2)
        rows = [
            {"chunk_text": "x" * 50000, "vector": [0.0, 0.0]},
            {"chunk_text": "short", "vector": [0.1, 0.2]},
        ]
        with patch.object(vs.MilvusClient, "query", MagicMock(return_value=rows)):
            samples = await adapter.sample_chunks(3)
        assert [t for t, _ in samples] == ["short"]


class TestDropIndex:
    async def test_calls_drop_collection(self, mock_client):
        from treeweft.adapters.milvus import vector_store as vs
        adapter = _make_adapter(mock_client)
        with patch.object(vs.MilvusClient, "drop_collection", MagicMock()) as drop:
            await adapter.drop_index()
        drop.assert_called_once_with(mock_client, adapter.collection_name)
