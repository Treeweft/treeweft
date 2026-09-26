"""LanceDB summary-vector rewrite functions (ADR-003 §3, research R7/R10).

Real embedded LanceDB in a tmp dir — no mocking (constitution II allows
embedded, in-process stores).
"""
import pytest

from treeweft.adapters.lancedb import vector_store as vs

DIM = 8


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "LANCEDB_PATH", str(tmp_path / "lancedb"))
    monkeypatch.setattr(vs, "TABLE_NAME", "test_chunks")
    monkeypatch.setattr(vs, "VECTOR_DIM", DIM)
    monkeypatch.setattr(vs, "_db", None)
    monkeypatch.setattr(vs, "_table", None)
    yield


def _vec(seed: int) -> list[float]:
    return [((seed * 17 + i * 13) % 100) / 100.0 for i in range(DIM)]


def _chunk(i: int, text: str | None = None, source_id: str = "src-1") -> dict:
    return {
        "text": text or f"def func_{i}():\n    return {i}",
        "file_path": f"/repo/src/mod_{i % 3}/file_{i}.py",
        "language": "python",
        "start_line": 1 + i,
        "end_line": 10 + i,
        "source_id": source_id,
    }


async def _seed(n: int = 10, **kw):
    await vs.init_collection()
    chunks = [_chunk(i, **kw) for i in range(n)]
    await vs.insert_chunks(chunks, [_vec(i) for i in range(n)])
    return chunks


class TestSnapshotSourceRowIds:
    async def test_returns_only_that_sources_ids(self):
        await _seed(4, source_id="src-a")
        await vs.insert_chunks([_chunk(50, source_id="src-b")], [_vec(50)])

        ids_a = await vs.snapshot_source_row_ids("src-a")
        ids_b = await vs.snapshot_source_row_ids("src-b")

        tbl = vs._get_table()
        all_ids_a = {
            r["id"] for r in tbl.search(None).where("source_id = 'src-a'").limit(10).to_list()
        }
        assert set(ids_a) == all_ids_a
        assert len(ids_a) == 4
        assert len(ids_b) == 1

    async def test_unknown_source_returns_empty(self):
        await _seed(2)
        assert await vs.snapshot_source_row_ids("nope") == []


class TestFetchRows:
    async def test_returns_full_rows_for_ids(self):
        await _seed(3)
        ids = await vs.snapshot_source_row_ids("src-1")
        rows = await vs.fetch_rows(ids)

        assert {r["id"] for r in rows} == set(ids)
        for row in rows:
            assert "chunk_text" in row
            assert "vector" in row
            assert "file_path" in row
            assert "source_id" in row

    async def test_empty_input_returns_empty(self):
        await _seed(2)
        assert await vs.fetch_rows([]) == []

    async def test_ids_are_escaped(self):
        await _seed(2)
        assert await vs.fetch_rows(["x' OR '1'='1"]) == []


class TestWriteSummaryVectors:
    async def test_updates_in_place_and_none_stays_null(self):
        await _seed(4)
        ids_before = await vs.snapshot_source_row_ids("src-1")
        rows = await vs.fetch_rows(ids_before)
        rows.sort(key=lambda r: r["id"])

        new_vec = _vec(999)
        vectors = [new_vec, None, _vec(998), None]
        await vs.write_summary_vectors(rows, vectors)

        tbl = vs._get_table()
        after = {r["id"]: r for r in tbl.search(None).limit(10).to_list()}

        assert set(after) == set(ids_before)
        assert len(after) == 4

        for row, vec in zip(rows, vectors):
            updated = after[row["id"]]
            if vec is None:
                assert updated["summary_vector"] is None
            else:
                assert list(updated["summary_vector"]) == pytest.approx(vec)
            # unchanged fields
            assert list(updated["vector"]) == pytest.approx(list(row["vector"]))
            assert updated["chunk_text"] == row["chunk_text"]
            assert updated["file_path"] == row["file_path"]
            assert updated["source_id"] == row["source_id"]

    async def test_fts_search_still_finds_chunk_after_rewrite(self):
        chunks = [_chunk(i) for i in range(5)]
        chunks[2]["text"] = "def frobnicate_zanzibar():\n    pass"
        await vs.init_collection()
        await vs.insert_chunks(chunks, [_vec(i) for i in range(5)])

        ids = await vs.snapshot_source_row_ids("src-1")
        rows = await vs.fetch_rows(ids)
        vectors = [_vec(900 + i) for i in range(len(rows))]
        await vs.write_summary_vectors(rows, vectors)

        hits = vs.hybrid_search("frobnicate_zanzibar", _vec(0), top_k=5)
        paths = [h["entity"]["file_path"] for h in hits]
        assert "/repo/src/mod_2/file_2.py" in paths

    async def test_empty_rows_is_noop(self):
        await _seed(2)
        await vs.write_summary_vectors([], [])
        assert vs._get_table().count_rows() == 2


class TestCountSourceRows:
    async def test_matches_source_row_count(self):
        await _seed(3, source_id="src-a")
        await vs.insert_chunks([_chunk(60, source_id="src-b")], [_vec(60)])

        assert await vs.count_source_rows("src-a") == 3
        assert await vs.count_source_rows("src-b") == 1
        assert await vs.count_source_rows("nope") == 0


pytestmark = pytest.mark.asyncio
