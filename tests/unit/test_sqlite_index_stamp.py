"""SQLite graph store index stamp (ADR-004 §3): the treeweft_meta table."""
import pytest
import pytest_asyncio

from treeweft.adapters.sqlite import graph_store as gs
from treeweft.domain.index_stamp import IndexStamp, UnreadableStamp


@pytest_asyncio.fixture(autouse=True)
async def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "GRAPH_DB_PATH", str(tmp_path / "graph.db"))
    monkeypatch.setattr(gs, "_conn", None)
    monkeypatch.setattr(gs, "_conn_lock", None)
    await gs.ensure_schema()
    yield
    await gs.close()


STAMP = IndexStamp(schema=1, embedding_model="Qwen/Qwen3-Embedding-0.6B", vector_dim=1024)


async def test_ensure_schema_creates_treeweft_meta():
    conn = await gs._get_conn()
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='treeweft_meta'"
    )
    assert await cur.fetchone() is not None


async def test_read_stamp_is_none_when_no_row():
    assert await gs.read_stamp() is None


async def test_write_then_read_stamp_round_trips():
    await gs.write_stamp(STAMP)
    assert await gs.read_stamp() == STAMP


async def test_second_write_upserts_not_duplicates():
    await gs.write_stamp(STAMP)
    other = IndexStamp(schema=1, embedding_model="other-model", vector_dim=768)
    await gs.write_stamp(other)
    assert await gs.read_stamp() == other
    conn = await gs._get_conn()
    cur = await conn.execute("SELECT COUNT(*) AS n FROM treeweft_meta")
    row = await cur.fetchone()
    assert row["n"] == 1


async def test_observe_index_has_no_data_when_empty():
    obs = await gs.observe_index()
    assert obs.has_data is False
    assert obs.store == "graph"
    assert obs.backend == "sqlite"
    assert obs.exists is True


async def test_observe_index_has_data_after_one_entity():
    conn = await gs._get_conn()
    await conn.execute(
        "INSERT INTO entities (id, type, name, updated_at) VALUES (?, ?, ?, ?)",
        ("e1", "Function", "f", 0),
    )
    await conn.commit()
    obs = await gs.observe_index()
    assert obs.has_data is True


async def test_observe_index_reports_the_stamp():
    await gs.write_stamp(STAMP)
    obs = await gs.observe_index()
    assert obs.stamp == STAMP


async def test_clear_index_data_removes_entities_and_communities_keeps_stamp():
    conn = await gs._get_conn()
    await conn.execute(
        "INSERT INTO entities (id, type, name, updated_at) VALUES (?, ?, ?, ?)",
        ("e1", "Function", "f", 0),
    )
    await conn.execute(
        "INSERT INTO communities (id, summary, embedding) VALUES (?, ?, ?)",
        ("c1", "s", "[]"),
    )
    await conn.commit()
    await gs.write_stamp(STAMP)

    await gs.clear_index_data()

    obs = await gs.observe_index()
    assert obs.has_data is False
    cur = await conn.execute("SELECT COUNT(*) AS n FROM communities")
    row = await cur.fetchone()
    assert row["n"] == 0
    assert await gs.read_stamp() == STAMP


async def test_clear_all_keeps_the_stamp():
    await gs.write_stamp(STAMP)
    await gs.clear_all()
    assert await gs.read_stamp() == STAMP


async def test_non_integer_index_schema_makes_observe_index_unreadable():
    conn = await gs._get_conn()
    await conn.execute(
        "INSERT INTO treeweft_meta (id, index_schema, embedding_model, vector_dim, stamped_at) "
        "VALUES ('index', 1, 'x', 1024, 0)"
    )
    await conn.commit()
    # Corrupt it directly (bypassing the type-checked write path).
    await conn.execute("UPDATE treeweft_meta SET index_schema = 'not-a-number' WHERE id='index'")
    await conn.commit()
    obs = await gs.observe_index()
    assert isinstance(obs.stamp, UnreadableStamp)


pytestmark = pytest.mark.asyncio
