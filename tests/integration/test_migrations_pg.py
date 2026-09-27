"""`run_migrations()` against a real Postgres.

Proves what the unit tests (`tests/unit/test_run_migrations.py`, against a
fake pool) cannot: that a failed migration really is rolled back by the
server and leaves no tracking row; that every shipped migration applies
inside the runner's transaction on an empty database; that two runners
started together apply each migration exactly once, whatever the
database's default isolation level; and that a role without CREATE on the
schema can start when nothing is pending.

Every test runs in its own database, `tw_migtest_<hex>`, created here and
dropped afterwards — the runner's tracking table has a fixed name, so
sharing a database with another suite would mix their rows. The Postgres
role therefore needs CREATEDB, and CREATEROLE for the
restricted-role test; without them the module fails loudly at setup rather
than skipping silently.

Gets its Postgres address from the `postgres_url` fixture (conftest.py):
explicit POSTGRES_TEST_URL, else a throwaway testcontainer when
TREEWEFT_ITEST_CONTAINERS=1, else skipped. A throwaway testcontainer's role is a
superuser.
"""
from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

pytestmark = [
    pytest.mark.slow,
    pytest.mark.asyncio,
]


async def _admin(sql: str, url: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


def _with(url: str, *, database: str | None = None, userinfo: str | None = None) -> str:
    parts = urlsplit(url)
    if database is not None:
        parts = parts._replace(path=f"/{database}")
    if userinfo is not None:
        parts = parts._replace(netloc=f"{userinfo}@{parts.netloc.rpartition('@')[2]}")
    return urlunsplit(parts)


@pytest_asyncio.fixture
async def database(postgres_url: str):
    """URL of a fresh empty database, dropped afterwards."""
    name = f"tw_migtest_{uuid.uuid4().hex[:12]}"
    await _admin(f'CREATE DATABASE "{name}"', postgres_url)
    try:
        yield _with(postgres_url, database=name)
    finally:
        await _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', postgres_url)


@pytest_asyncio.fixture
async def pg(database, monkeypatch):
    """The postgresql adapter package, its pool on the fresh database."""
    from treeweft.adapters import postgresql
    from treeweft.adapters.postgresql import connection

    monkeypatch.setenv("DATABASE_URL", database)
    pool = await connection.init_pool(database)
    assert pool is not None, "could not connect to the database just created"
    try:
        yield postgresql
    finally:
        await connection.close_pool()


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
    return pg._migration_files()


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

    async def test_script_that_ends_the_transaction_unseen_is_rejected(
        self, pg, monkeypatch, tmp_path
    ):
        """The backstop behind the check of the script's text: a procedure
        that commits ends the runner's transaction from inside a CALL."""
        pool = await pg.get_pool()
        await pool.execute(
            "CREATE PROCEDURE commits_inside() LANGUAGE plpgsql AS "
            "$$ BEGIN CREATE TABLE t_one (id int); COMMIT; END; $$"
        )
        monkeypatch.setattr(pg, "_MIGRATIONS_DIR", tmp_path)
        (tmp_path / "001_calls_proc.sql").write_text("CALL commits_inside();")
        (tmp_path / "002_after.sql").write_text("CREATE TABLE t_two (id int);")

        with pytest.raises(pg.MigrationError, match="001_calls_proc.sql"):
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

    async def test_two_runners_under_a_repeatable_read_default(self, database, pg, postgres_url):
        """The re-check under the lock must see the other runner's row even
        when the database defaults to an isolation level whose snapshot
        predates the wait for the lock."""
        from treeweft.adapters.postgresql import connection

        name = urlsplit(database).path.lstrip("/")
        await _admin(
            f'ALTER DATABASE "{name}" SET default_transaction_isolation = \'repeatable read\'',
            postgres_url,
        )
        await connection.close_pool()  # the setting applies to new sessions
        await connection.init_pool(database)
        pool = await pg.get_pool()
        assert await pool.fetchval("SHOW default_transaction_isolation") == "repeatable read"

        first, second = await asyncio.gather(pg.run_migrations(), pg.run_migrations())

        assert sorted(first + second) == _shipped(pg)
        assert await _recorded(pg) == _shipped(pg)


class TestRestrictedRole:
    async def test_role_without_create_starts_when_nothing_is_pending(
        self, database, pg, postgres_url
    ):
        """Schema provisioned by an owner role, indexer connecting with
        read/write rights only: CREATE TABLE IF NOT EXISTS would be refused
        even though the tracking table exists."""
        from treeweft.adapters.postgresql import connection

        await pg.run_migrations()
        await connection.close_pool()

        role = f"tw_migtest_role_{uuid.uuid4().hex[:12]}"
        await _admin(f"CREATE ROLE {role} LOGIN PASSWORD '{role}'", postgres_url)
        try:
            await _admin(
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC;"
                f"GRANT USAGE ON SCHEMA public TO {role};"
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role};",
                database,
            )
            pool = await connection.init_pool(_with(database, userinfo=f"{role}:{role}"))
            assert pool is not None
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await pool.execute("CREATE TABLE t_probe (id int)")

            assert await pg.run_migrations() == []
        finally:
            await connection.close_pool()
            await _admin(f"DROP OWNED BY {role}", database)
            await _admin(f"DROP ROLE {role}", postgres_url)
