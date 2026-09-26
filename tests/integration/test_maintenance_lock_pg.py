"""The Postgres advisory maintenance lock against a real Postgres (ADR-004
§3, research R13). Proves two genuinely separate connections see the same
advisory lock consistently, and that a crashed holder (connection closed
without `release()`) is released by Postgres itself — the mechanism that
turns a crashed rebuild into `interrupted` with no timeout.

Uses only the advisory-lock key; creates no tables, needs no migrations.

Gets its Postgres address from the `postgres_url` fixture (conftest.py):
explicit POSTGRES_TEST_URL, else a throwaway testcontainer when
TREEWEFT_ITEST_CONTAINERS=1, else skipped.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

pytestmark = [
    pytest.mark.slow,
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def lock(postgres_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    from treeweft.adapters.postgresql import connection, maintenance_lock

    await connection.init_pool(postgres_url)
    yield maintenance_lock
    await connection.close_pool()


class TestExclusiveSeenByProbe:
    async def test_exclusive_lock_from_one_connection_is_seen_by_probe_on_another(self, lock):
        handle = await lock.acquire("exclusive")
        assert handle is not None
        assert handle.coordinated is True
        try:
            mode = await lock.probe()
            assert mode == "exclusive"
        finally:
            await handle.release()

        mode = await lock.probe()
        assert mode is None


class TestModeConflicts:
    async def test_shared_refused_while_exclusive_held(self, lock):
        exclusive = await lock.acquire("exclusive")
        assert exclusive is not None
        try:
            denied = await lock.acquire("shared")
            assert denied is None
        finally:
            await exclusive.release()

    async def test_exclusive_refused_while_shared_held(self, lock):
        shared = await lock.acquire("shared")
        assert shared is not None
        try:
            denied = await lock.acquire("exclusive")
            assert denied is None
        finally:
            await shared.release()


class TestCrashedHolderReleasesAutomatically:
    async def test_closing_the_connection_without_release_frees_the_lock(self, lock, postgres_url: str):
        import asyncpg

        conn = await asyncpg.connect(postgres_url)
        granted = await conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtext('treeweft'), hashtext('index-maintenance'))"
        )
        assert granted is True

        assert await lock.probe() == "exclusive"

        await conn.close()  # simulate a crash: no pg_advisory_unlock call

        assert await lock.probe() is None
