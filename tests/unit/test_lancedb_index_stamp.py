"""LanceDB vector store index stamp (ADR-004 §3): the vector field's metadata.

Real embedded LanceDB in a tmp dir — no mocking (constitution II allows
embedded, in-process stores).
"""
import warnings

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
        tbl.update_field_metadata(
            {"path": "vector", "metadata": {"unrelated.key": "keep-me"}}
        )

        await vs.write_stamp(STAMP)

        meta = vs._current_stamp_metadata(tbl)
        assert meta.get("unrelated.key") == "keep-me"
        obs = await vs.observe_index()
        assert obs.stamp == STAMP


class TestRestampOverwritesStampKeysOnly:
    async def test_overwrites_both_stamp_keys_and_keeps_others(self):
        await vs.init_collection()
        tbl = vs._get_table()
        tbl.update_field_metadata(
            {"path": "vector", "metadata": {"unrelated.key": "keep-me"}}
        )
        old = IndexStamp(schema=1, embedding_model="old-model", vector_dim=DIM)
        await vs.write_stamp(old)
        new = IndexStamp(schema=2, embedding_model="new-model", vector_dim=DIM)
        await vs.write_stamp(new)

        meta = vs._current_stamp_metadata(tbl)
        assert meta["unrelated.key"] == "keep-me"
        assert meta[vs._STAMP_SCHEMA_KEY] == "2"
        assert meta[vs._STAMP_MODEL_KEY] == "new-model"
        assert (await vs.observe_index()).stamp == new

    async def test_create_reopen_and_stamp_emit_no_deprecation_warning(self):
        """Calls lancedb directly (not through `asyncio.to_thread`) so the
        warnings filter is in force in the thread that calls lancedb."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            tbl = vs._get_table()  # create path: table + FTS index + stamp
            vs._stamp_field_metadata(tbl, STAMP)
            vs._db = None
            vs._table = None
            vs._get_table()  # reopen path

    @pytest.mark.parametrize("bad", [
        IndexStamp(schema=1, embedding_model=None, vector_dim=DIM),
        IndexStamp(schema=1, embedding_model="", vector_dim=DIM),
    ])
    async def test_refuses_an_empty_stamp_value(self, bad):
        """update_field_metadata treats None as "delete this key", so an empty
        value must fail loudly rather than silently erase the stamp."""
        tbl = vs._get_table()
        vs._stamp_field_metadata(tbl, STAMP)
        with pytest.raises(ValueError):
            vs._stamp_field_metadata(tbl, bad)
        assert vs._current_stamp_metadata(tbl)[vs._STAMP_MODEL_KEY] == MODEL


def _fts_indices(tbl):
    return [i for i in tbl.list_indices() if i.columns == ["chunk_text"]]


def _old_api_table(stamp: IndexStamp):
    """A table as the pre-#96 code built it: deprecated stamp + FTS calls."""
    import lancedb

    db = lancedb.connect(vs.LANCEDB_PATH)
    tbl = db.create_table(vs.TABLE_NAME, schema=vs._schema())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tbl.replace_field_metadata(
            "vector",
            {
                vs._STAMP_SCHEMA_KEY: str(stamp.schema),
                vs._STAMP_MODEL_KEY: stamp.embedding_model,
            },
        )
        tbl.create_fts_index("chunk_text", use_tantivy=False)
    return tbl


class TestOldTableCompatibility:
    async def test_opens_old_table_keeps_stamp_and_adds_no_second_index(self):
        _old_api_table(STAMP)
        before = [(i.name, i.index_type) for i in _fts_indices(_reopen())]
        assert len(before) == 1

        vs._db = None
        vs._table = None
        tbl = vs._get_table()

        after = [(i.name, i.index_type) for i in _fts_indices(tbl)]
        assert after == before
        assert (await vs.observe_index()).stamp == STAMP


def _reopen():
    import lancedb

    return lancedb.connect(vs.LANCEDB_PATH).open_table(vs.TABLE_NAME)


class TestHybridSearchParity:
    """The old and new FTS index must rank identically. Rows are inserted and
    then `optimize()`d so the BM25 leg reads the *index* (an index built on an
    empty table leaves every later row unindexed, and a flat scan would make
    any two indexes look equal). The corpus has a distinct query-term count
    per row so no two BM25 scores tie and the order isn't up to lancedb's
    tie-breaking."""

    ROWS = [
        {"text": "widget " * (i + 1) + "parse " + "filler " * (12 - i) + f"handler_{i}",
         "file_path": f"/r/f{i}.py", "language": "python",
         "start_line": 1, "end_line": 2, "source_id": "s1"}
        for i in range(12)
    ]

    @staticmethod
    async def _build(use_old: bool, path, monkeypatch):
        monkeypatch.setattr(vs, "LANCEDB_PATH", str(path))
        monkeypatch.setattr(vs, "_db", None)
        monkeypatch.setattr(vs, "_table", None)
        if use_old:
            _old_api_table(STAMP)
        await vs.insert_chunks(TestHybridSearchParity.ROWS, [_vec(i) for i in range(12)])
        tbl = vs._get_table()
        tbl.optimize()
        (idx,) = _fts_indices(tbl)
        stats = tbl.index_stats(idx.name)
        assert stats.num_indexed_rows == 12 and stats.num_unindexed_rows == 0
        return tbl

    async def test_bm25_scores_and_hybrid_ranking_match(self, tmp_path, monkeypatch):
        results = {}
        for use_old in (True, False):
            tbl = await self._build(use_old, tmp_path / ("old" if use_old else "new"), monkeypatch)
            bm25 = [
                (r["file_path"], round(r["_score"], 6))
                for r in tbl.search("widget parse", query_type="fts").limit(12).to_list()
            ]
            hybrid = [h["entity"]["file_path"]
                      for h in vs.hybrid_search("widget parse", _vec(3), top_k=8)]
            results[use_old] = (bm25, hybrid)

        old_bm25, old_hybrid = results[True]
        new_bm25, new_hybrid = results[False]
        scores = [s for _, s in new_bm25]
        assert len(scores) == 12 and len(set(scores)) == 12, "BM25 scores tie; fix the corpus"
        assert old_bm25 == new_bm25
        assert old_hybrid and old_hybrid == new_hybrid


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
