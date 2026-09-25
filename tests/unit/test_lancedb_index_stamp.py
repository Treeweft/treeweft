"""LanceDB vector store index stamp (ADR-004 §3): the vector field's metadata.

Real embedded LanceDB in a tmp dir — no mocking (constitution II allows
embedded, in-process stores).
"""
import pytest

from treeweft.adapters.lancedb import vector_store as vs
from treeweft.domain.index_stamp import IndexStamp

DIM = 8
MODEL = "Qwen/Qwen3-Embedding-0.6B"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "LANCEDB_PATH", str(tmp_path / "lancedb"))
    monkeypatch.setattr(vs, "TABLE_NAME", "test_chunks")
    monkeypatch.setattr(vs, "VECTOR_DIM", DIM)
    monkeypatch.setattr(vs, "EMBEDDING_MODEL", MODEL)
    monkeypatch.setattr(vs, "_db", None)
    monkeypatch.setattr(vs, "_table", None)
    yield


STAMP = IndexStamp(schema=1, embedding_model=MODEL, vector_dim=DIM)


def _vec(seed: int) -> list[float]:
    return [((seed * 17 + i * 13) % 100) / 100.0 for i in range(DIM)]


class TestObserveIndexNoTable:
    async def test_missing_table_reports_not_exists(self):
        obs = await vs.observe_index()
        assert obs.exists is False
        assert obs.store == "vector"
        assert obs.backend == "lancedb"


class TestInitCollectionStamps:
    async def test_creating_the_table_stamps_it(self):
        await vs.init_collection()
        obs = await vs.observe_index()
        assert obs.exists is True
        assert obs.stamp == STAMP


class TestWriteStampMerges:
    async def test_round_trip(self):
        await vs.init_collection()
        other = IndexStamp(schema=1, embedding_model="other-model", vector_dim=DIM)
        await vs.write_stamp(other)
        obs = await vs.observe_index()
        assert obs.stamp == other

    async def test_merges_with_an_unrelated_existing_key(self):
        await vs.init_collection()
        tbl = vs._get_table()
        existing = vs._current_stamp_metadata(tbl)
        existing["unrelated.key"] = "keep-me"
        tbl.replace_field_metadata("vector", existing)

        await vs.write_stamp(STAMP)

        meta = vs._current_stamp_metadata(tbl)
        assert meta.get("unrelated.key") == "keep-me"
        obs = await vs.observe_index()
        assert obs.stamp == STAMP


class TestStampSurvivesLifecycle:
    async def test_survives_insert_delete_fts_optimize_reopen(self):
        await vs.init_collection()
        await vs.write_stamp(STAMP)
        await vs.insert_chunks(
            [{"text": "def f(): pass", "file_path": "/a.py", "language": "python",
              "start_line": 1, "end_line": 1, "source_id": "s1"}],
            [_vec(1)],
        )
        vs.delete_chunks_by_source("s1")
        tbl = vs._get_table()
        tbl.optimize()

        # Reopen fresh, simulating a new process.
        vs._db = None
        vs._table = None
        obs = await vs.observe_index()
        assert obs.stamp == STAMP


class TestSchemaDim:
    async def test_schema_dim_from_list_size(self):
        await vs.init_collection()
        obs = await vs.observe_index()
        assert obs.schema_dim == DIM


class TestSampleChunks:
    async def test_returns_text_vector_pairs(self):
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

    async def test_skips_rows_at_or_over_50000_chars(self):
        await vs.init_collection()
        long_text = "x" * 50000
        short_text = "short"
        await vs.insert_chunks(
            [
                {"text": long_text, "file_path": "/a.py", "language": "python",
                 "start_line": 1, "end_line": 1, "source_id": "s1"},
                {"text": short_text, "file_path": "/b.py", "language": "python",
                 "start_line": 1, "end_line": 1, "source_id": "s1"},
            ],
            [_vec(1), _vec(2)],
        )
        samples = await vs.sample_chunks(3)
        texts = [t for t, _ in samples]
        assert short_text in texts
        assert long_text not in texts


class TestDropIndex:
    async def test_removes_the_table(self):
        await vs.init_collection()
        await vs.drop_index()
        obs = await vs.observe_index()
        assert obs.exists is False


pytestmark = pytest.mark.asyncio
