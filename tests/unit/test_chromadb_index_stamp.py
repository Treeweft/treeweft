"""ChromaDB vector store index stamp (ADR-004 §3): collection metadata.

Real embedded PersistentClient in a tmp dir (constitution II allows embedded,
in-process stores).
"""
import pytest

from treeweft.adapters.chromadb import vector_store as vs
from treeweft.domain.index_stamp import IndexStamp

DIM = 8
MODEL = "Qwen/Qwen3-Embedding-0.6B"
STAMP = IndexStamp(schema=1, embedding_model=MODEL, vector_dim=DIM)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setattr(vs, "VECTOR_DIM", DIM)
    monkeypatch.setattr(vs, "EMBEDDING_MODEL", MODEL)
    monkeypatch.setattr(vs, "_adapter", None)
    yield


def _vec(seed: int) -> list[float]:
    return [((seed * 17 + i * 13) % 100) / 100.0 for i in range(DIM)]


class TestInitCollectionStamps:
    async def test_creating_the_collection_stamps_its_metadata(self):
        await vs.init_collection()
        obs = await vs.observe_index()
        assert obs.exists is True
        assert obs.stamp == STAMP


class TestObserveIndexNoCollection:
    async def test_missing_collection_reports_not_exists(self):
        obs = await vs.observe_index()
        assert obs.exists is False


class TestWriteStampKeepsExistingKeys:
    async def test_keeps_an_unrelated_metadata_key(self):
        await vs.init_collection()
        adapter = vs._get_adapter()
        adapter._collection.modify(metadata={"unrelated.key": "keep-me"})

        await vs.write_stamp(STAMP)

        meta = adapter._collection.metadata
        assert meta.get("unrelated.key") == "keep-me"
        obs = await vs.observe_index()
        assert obs.stamp == STAMP

    async def test_round_trip_after_change(self):
        await vs.init_collection()
        other = IndexStamp(schema=1, embedding_model="other-model", vector_dim=DIM)
        await vs.write_stamp(other)
        obs = await vs.observe_index()
        assert obs.stamp == other


class TestSampleChunksAndDrop:
    async def test_sample_chunks_returns_documents_and_embeddings(self):
        await vs.init_collection()
        await vs.insert_chunks(
            [{"text": "short chunk", "file_path": "/a.py", "language": "python",
              "start_line": 1, "end_line": 1, "source_id": "s1"}],
            [_vec(1)],
        )
        samples = await vs.sample_chunks(3)
        assert len(samples) == 1
        text, vec = samples[0]
        assert text == "short chunk"
        assert len(vec) == DIM

    async def test_drop_index_removes_the_collection(self):
        await vs.init_collection()
        await vs.drop_index()
        obs = await vs.observe_index()
        assert obs.exists is False


pytestmark = pytest.mark.asyncio
