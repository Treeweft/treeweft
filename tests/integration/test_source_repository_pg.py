"""PostgreSourceRepository against a real Postgres (#51).

save() wrote ten columns and never graph_indexed, which defaults to TRUE, so
every source read back as graph-indexed whatever its job did: a chunks-only
(skip_graph) run, and rebuild_all_graphs(only_missing=True), could not work.
Runs every migration on a fresh database, then round-trips records.

Gets its Postgres address from the `postgres_url` fixture (conftest.py):
explicit POSTGRES_TEST_URL, else a throwaway testcontainer when
TREEWEFT_ITEST_CONTAINERS=1, else skipped. The role needs CREATEDB.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [
    pytest.mark.slow,
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def repo(postgres_url: str, monkeypatch):
    from treeweft.adapters import postgresql
    from treeweft.adapters.postgresql import connection
    from treeweft.adapters.sources.repository import PostgreSourceRepository

    name = f"tw_srctest_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(postgres_url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    url = urlunsplit(urlsplit(postgres_url)._replace(path=f"/{name}"))
    try:
        monkeypatch.setenv("DATABASE_URL", url)
        pool = await connection.init_pool(url)
        assert pool is not None
        await postgresql.run_migrations()
        yield PostgreSourceRepository(pool=pool)
    finally:
        await connection.close_pool()
        admin = await asyncpg.connect(postgres_url)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


def _record(**overrides):
    from treeweft.domain.sources import SourceRecord

    fields = dict(
        id=f"src-{uuid.uuid4().hex[:8]}", path="/repo", url="", branch="main",
        indexed_at=datetime(2026, 9, 27, tzinfo=timezone.utc), file_count=3, chunk_count=9,
    )
    fields.update(overrides)
    return SourceRecord(**fields)


async def test_a_chunks_only_source_reads_back_as_not_graph_indexed(repo):
    record = _record(graph_indexed=False, graph_indexed_at=None)

    await repo.save(record)

    stored = await repo.get_by_id(record.id)
    assert stored.graph_indexed is False
    assert stored.graph_indexed_at is None


async def test_saving_again_updates_graph_indexed_both_ways(repo):
    when = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    record = _record(graph_indexed=True, graph_indexed_at=when)
    await repo.save(record)
    assert (await repo.get_by_id(record.id)).graph_indexed is True

    await repo.save(_record(id=record.id, graph_indexed=False, graph_indexed_at=None))
    stored = await repo.get_by_id(record.id)
    assert stored.graph_indexed is False

    await repo.save(_record(id=record.id, graph_indexed=True, graph_indexed_at=when))
    stored = await repo.get_by_id(record.id)
    assert stored.graph_indexed is True
    assert stored.graph_indexed_at == when
