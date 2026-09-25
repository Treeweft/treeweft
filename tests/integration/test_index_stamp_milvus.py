"""Index stamp round-trip against a real Milvus (ADR-004 §3, research R12).

Proves what pymilvus 3.0.0 / Milvus itself actually do with the stamp
properties and the sampled-row query — the unit tests
(tests/unit/test_milvus_index_stamp.py) only prove the client calls are
shaped right. Uses a uniquely named collection built directly from
`MilvusAdapter(collection_name=...)`, never the module-level wrappers
(those address the configured MILVUS_COLLECTION).

Run against a standalone instance:

    docker compose --profile local-infra up -d milvus
    MILVUS_TEST_URI=http://localhost:19530 \
      env -u PYTHONPATH python -m pytest tests/integration/test_index_stamp_milvus.py -v

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

COLL = f"itest_stamp_{uuid.uuid4().hex[:8]}"
DIM = 4
MODEL = "Qwen/Qwen3-Embedding-0.6B"


@pytest.fixture
def adapter():
    os.environ.setdefault("VECTOR_DIM", str(DIM))
    os.environ.setdefault("EMBEDDING_MODEL", MODEL)
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


class TestCreateStampsWithProperties:
    async def test_init_collection_creates_and_stamps(self, adapter):
        await adapter.init_collection()
        obs = await adapter.observe_index()
        assert obs.exists is True
        assert obs.stamp is not None
        assert obs.stamp.schema == 1
        assert obs.stamp.embedding_model == MODEL

    async def test_properties_round_trip_via_describe_collection(self, adapter):
        """This is the load-bearing check: research R1 marked whether Milvus
        2.5.4 accepts arbitrary treeweft.* collection properties as
        unverified — this proves it does."""
        await adapter.init_collection()
        mc = adapter._get_client()
        desc = mc.describe_collection(COLL)
        props = desc.get("properties", {})
        assert props.get("treeweft.index_schema") == "1"
        assert props.get("treeweft.embedding_model") == MODEL


class TestAlterOnExistingCollection:
    async def test_write_stamp_updates_properties(self, adapter):
        await adapter.init_collection()
        from treeweft.domain.index_stamp import IndexStamp

        other = IndexStamp(schema=1, embedding_model="other-model", vector_dim=DIM)
        await adapter.write_stamp(other)

        obs = await adapter.observe_index()
        assert obs.stamp == other


class TestDimensionFromSchema:
    async def test_schema_dim_matches_the_field(self, adapter):
        await adapter.init_collection()
        obs = await adapter.observe_index()
        assert obs.schema_dim == DIM


class TestSampleChunksOverRealRows:
    async def test_sample_chunks_returns_inserted_rows(self, adapter):
        await adapter.init_collection()
        await adapter.insert(
            [{"text": "short chunk", "file_path": "/a.py", "language": "python",
              "start_line": 1, "end_line": 1, "source_id": "s1"}],
            [[0.1, 0.2, 0.3, 0.4]],
        )
        mc = adapter._get_client()
        mc.flush(COLL)
        mc.load_collection(COLL)

        samples = await adapter.sample_chunks(3)
        assert len(samples) == 1
        text, vec = samples[0]
        assert text == "short chunk"
        assert len(vec) == DIM

    async def test_has_data_after_insert(self, adapter):
        await adapter.init_collection()
        await adapter.insert(
            [{"text": "x", "file_path": "/a.py", "language": "python",
              "start_line": 1, "end_line": 1, "source_id": "s1"}],
            [[0.1, 0.2, 0.3, 0.4]],
        )
        mc = adapter._get_client()
        mc.flush(COLL)
        obs = await adapter.observe_index()
        assert obs.has_data is True


class TestDropIndex:
    async def test_drop_removes_the_collection(self, adapter):
        await adapter.init_collection()
        await adapter.drop_index()
        obs = await adapter.observe_index()
        assert obs.exists is False
