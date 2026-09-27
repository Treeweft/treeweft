"""`run_migrations()` against a real Postgres.

Proves what the unit tests (`tests/unit/test_run_migrations.py`, against a
fake pool) cannot: that a failed migration really is rolled back by the
server and leaves no tracking row; that every shipped migration applies
inside the runner's transaction on an empty database; that a script
carrying its own COMMIT really does end that transaction, and is rejected;
and that two runners started together apply each migration exactly once.

Every test runs in its own database, `tw_migtest_<hex>`, created here and
dropped afterwards — the runner's tracking table has a fixed name, so
sharing a database with another suite would mix their rows. The role in
POSTGRES_TEST_URL therefore needs CREATEDB; without it the module fails
loudly at setup rather than skipping silently.

Run against a standalone instance:

    POSTGRES_TEST_URL=postgresql://user:pass@localhost:5432/treeweft_test \\
      env -u PYTHONPATH python -m pytest tests/integration/test_migrations_pg.py -v

Skipped unless POSTGRES_TEST_URL is set — no service, no silent pass.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

URL = os.environ.get("POSTGRES_TEST_URL")
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not URL, reason="set POSTGRES_TEST_URL to run"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def pg(monkeypatch):
    """The postgresql adapter package, its pool on a fresh empty database."""
    from treeweft.adapters import postgresql
    from treeweft.adapters.postgresql import connection

    assert URL is not None  # the skipif above
    name = f"tw_migtest_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(URL)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()

    db_url = urlunsplit(urlsplit(URL)._replace(path=f"/{name}"))
    monkeypatch.setenv("DATABASE_URL", db_url)
    pool = await connection.init_pool(db_url)
    assert pool is not None, f"could not connect to the database just created ({name})"
    try:
        yield postgresql
    finally:
        await connection.close_pool()
        admin = await asyncpg.connect(URL)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


async def _tables(pg) -> list[str]:
    pool = await pg.get_pool()
    rows = await pool.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    return [r["tablename"] for r in rows]


async def _recorded(pg) -> list[str]:
    pool = await pg.get_pool()
    rows = await pool.fetch("SELECT name FROM treeweft_migrations ORDER BY name")
    return [r["name"] for r in rows]


def _shipped(pg) -> list[str]:
    return sorted(f for f in os.listdir(pg._MIGRATIONS_DIR) if f.endswith(".sql"))


class TestFailedMigration:
    async def test_stops_at_the_failure_and_resumes_after_the_fix(
        self, pg, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(pg, "_MIGRATIONS_DIR", tmp_path)
        (tmp_path / "001_ok.sql").write_text("CREATE TABLE t_one (id int);")
        (tmp_path / "002_broken.sql").write_text(
            "CREATE TABLE t_two (id int); ALTER TABLE does_not_exist ADD COLUMN x int;"
        )
        (tmp_path / "003_after.sql").write_text("CREATE TABLE t_three (id int);")

        with pytest.raises(pg.MigrationError) as excinfo:
            await pg.run_migrations()

        message = str(excinfo.value)
        assert "002_broken.sql" in message
        assert 'relation "does_not_exist" does not exist' in message
        assert isinstance(excinfo.value.__cause__, asyncpg.exceptions.UndefinedTableError)
        # 002's first statement was rolled back with the rest of it; 003 never ran.
        assert await _tables(pg) == ["t_one", "treeweft_migrations"]
        assert await _recorded(pg) == ["001_ok.sql"]

        (tmp_path / "002_broken.sql").write_text("CREATE TABLE t_two (id int);")
        applied = await pg.run_migrations()

        assert applied == ["002_broken.sql", "003_after.sql"]
        assert await _tables(pg) == ["t_one", "t_three", "t_two", "treeweft_migrations"]
        assert await _recorded(pg) == ["001_ok.sql", "002_broken.sql", "003_after.sql"]

    async def test_script_with_its_own_commit_is_rejected(self, pg, monkeypatch, tmp_path):
        monkeypatch.setattr(pg, "_MIGRATIONS_DIR", tmp_path)
        (tmp_path / "001_own_txn.sql").write_text(
            "BEGIN; CREATE TABLE t_one (id int); COMMIT;"
        )
        (tmp_path / "002_after.sql").write_text("CREATE TABLE t_two (id int);")

        with pytest.raises(pg.MigrationError, match="001_own_txn.sql"):
            await pg.run_migrations()

        assert await _recorded(pg) == []
        assert "t_two" not in await _tables(pg)


class TestShippedMigrations:
    async def test_all_apply_on_an_empty_database(self, pg):
        applied = await pg.run_migrations()

        assert applied == _shipped(pg)
        assert await _recorded(pg) == _shipped(pg)
        pool = await pg.get_pool()
        # Migration 008 lost its own BEGIN/COMMIT; its index must still land.
        assert await pool.fetchval(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'jobs_active_source_uniq'"
        ) == 1

    async def test_second_run_applies_nothing(self, pg):
        await pg.run_migrations()

        assert await pg.run_migrations() == []

    async def test_two_runners_started_together_apply_each_migration_once(self, pg):
        first, second = await asyncio.gather(pg.run_migrations(), pg.run_migrations())

        assert sorted(first + second) == _shipped(pg)
        assert await _recorded(pg) == _shipped(pg)
