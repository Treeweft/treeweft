"""Postgres advisory maintenance lock (ADR-004 §3, research R13).

Mocks asyncpg.connect and the pool — no real Postgres.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treeweft.adapters.postgresql import maintenance_lock as ml

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_warned(monkeypatch):
    monkeypatch.setattr(ml, "_warned_no_postgres", False)


def _pool(fetchval=None, fetchrow=None):
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _dedicated_conn(fetchval_return=True):
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    conn.close = AsyncMock()
    return conn


class TestAcquireGranted:
    async def test_exclusive_opens_a_dedicated_connection_not_pool_acquire(self):
        pool, _ = _pool()
        dedicated = _dedicated_conn(True)
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=pool)), \
             patch("treeweft.adapters.postgresql.maintenance_lock.asyncpg.connect", AsyncMock(return_value=dedicated)) as connect:
            handle = await ml.acquire("exclusive")
        connect.assert_called_once()
        pool.acquire.assert_not_called()
        assert handle is not None
        assert handle.coordinated is True
        assert handle.mode == "exclusive"

    async def test_exclusive_calls_pg_try_advisory_lock_with_the_key(self):
        pool, _ = _pool()
        dedicated = _dedicated_conn(True)
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=pool)), \
             patch("treeweft.adapters.postgresql.maintenance_lock.asyncpg.connect", AsyncMock(return_value=dedicated)):
            await ml.acquire("exclusive")
        query = dedicated.fetchval.call_args.args[0]
        assert "pg_try_advisory_lock(" in query
        assert "pg_try_advisory_lock_shared(" not in query
        assert "hashtext('treeweft')" in query
        assert "hashtext('index-maintenance')" in query

    async def test_shared_calls_pg_try_advisory_lock_shared(self):
        pool, _ = _pool()
        dedicated = _dedicated_conn(True)
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=pool)), \
             patch("treeweft.adapters.postgresql.maintenance_lock.asyncpg.connect", AsyncMock(return_value=dedicated)):
            handle = await ml.acquire("shared")
        query = dedicated.fetchval.call_args.args[0]
        assert "pg_try_advisory_lock_shared(" in query
        assert handle.mode == "shared"


class TestAcquireRefused:
    async def test_another_holder_conflicts_returns_none_and_closes_connection(self):
        pool, _ = _pool()
        dedicated = _dedicated_conn(False)
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=pool)), \
             patch("treeweft.adapters.postgresql.maintenance_lock.asyncpg.connect", AsyncMock(return_value=dedicated)):
            handle = await ml.acquire("exclusive")
        assert handle is None
        dedicated.close.assert_called_once()


class TestRelease:
    async def test_release_unlocks_and_closes(self):
        pool, _ = _pool()
        dedicated = _dedicated_conn(True)
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=pool)), \
             patch("treeweft.adapters.postgresql.maintenance_lock.asyncpg.connect", AsyncMock(return_value=dedicated)):
            handle = await ml.acquire("exclusive")
        await handle.release()
        unlock_query = dedicated.fetchval.call_args_list[-1].args[0]
        assert "pg_advisory_unlock(" in unlock_query
        dedicated.close.assert_called_once()

    async def test_release_is_idempotent(self):
        pool, _ = _pool()
        dedicated = _dedicated_conn(True)
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=pool)), \
             patch("treeweft.adapters.postgresql.maintenance_lock.asyncpg.connect", AsyncMock(return_value=dedicated)):
            handle = await ml.acquire("exclusive")
        await handle.release()
        await handle.release()  # must not double-close or raise
        assert dedicated.close.call_count == 1

    async def test_no_op_handle_release_does_nothing(self):
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=None)):
            handle = await ml.acquire("exclusive")
        await handle.release()  # must not raise


class TestProbe:
    async def test_reads_pg_locks_and_reports_exclusive(self):
        pool, conn = _pool(fetchrow={"mode": "ExclusiveLock"})
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=pool)):
            mode = await ml.probe()
        assert mode == "exclusive"
        query = conn.fetchrow.call_args.args[0]
        assert "pg_locks" in query
        assert "hashtext('treeweft')" in query
        assert "hashtext('index-maintenance')" in query
        assert "granted" in query

    async def test_reports_shared(self):
        pool, _ = _pool(fetchrow={"mode": "ShareLock"})
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=pool)):
            mode = await ml.probe()
        assert mode == "shared"

    async def test_unheld_is_none(self):
        pool, _ = _pool(fetchrow=None)
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=pool)):
            mode = await ml.probe()
        assert mode is None

    async def test_probe_never_takes_the_lock(self):
        pool, conn = _pool(fetchrow=None)
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=pool)):
            await ml.probe()
        for call in conn.method_calls:
            assert "advisory_lock" not in call[0]


class TestNoPostgres:
    async def test_acquire_returns_no_op_handle(self):
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=None)):
            handle = await ml.acquire("exclusive")
        assert handle is not None
        assert handle.coordinated is False

    async def test_probe_returns_none(self):
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=None)):
            assert await ml.probe() is None

    async def test_warns_once_per_process_not_once_per_call(self):
        with patch("treeweft.adapters.postgresql.maintenance_lock.get_pool", AsyncMock(return_value=None)), \
             patch("treeweft.adapters.postgresql.maintenance_lock.logger") as mock_logger:
            await ml.acquire("exclusive")
            await ml.acquire("shared")
            await ml.acquire("exclusive")
        assert mock_logger.warning.call_count == 1
