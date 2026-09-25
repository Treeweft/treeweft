"""JobGroupStore.latest_by_kind / delete (ADR-004 §3): the rebuild uses
these to find its group and abort cleanly.

Mocks only the asyncpg pool (out-of-process dependency), like
test_source_repository.py.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from treeweft.adapters.postgresql.job_group_store import JobGroupStore

pytestmark = pytest.mark.asyncio


def _pool(fetchrow=None, fetch=None):
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.execute = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _row(**overrides):
    base = {"id": "grp_1", "label": "index rebuild", "kind": "index-rebuild",
            "created_at": 1000.0, "created_by": "admin", "task_count": 3}
    base.update(overrides)
    return base


class TestLatestByKind:
    async def test_returns_the_most_recent_group_of_that_kind(self, monkeypatch):
        pool, conn = _pool(fetchrow=_row())
        monkeypatch.setattr(
            "treeweft.adapters.postgresql.job_group_store.get_pool", AsyncMock(return_value=pool)
        )
        store = JobGroupStore()
        group = await store.latest_by_kind("index-rebuild")
        assert group is not None
        assert group.id == "grp_1"
        assert group.kind == "index-rebuild"
        assert group.task_count == 3
        query, kind_arg = conn.fetchrow.call_args.args
        assert "ORDER BY created_at DESC" in query
        assert kind_arg == "index-rebuild"

    async def test_returns_none_when_no_group_of_that_kind(self, monkeypatch):
        pool, _ = _pool(fetchrow=None)
        monkeypatch.setattr(
            "treeweft.adapters.postgresql.job_group_store.get_pool", AsyncMock(return_value=pool)
        )
        store = JobGroupStore()
        assert await store.latest_by_kind("index-rebuild") is None

    async def test_no_pool_raises(self, monkeypatch):
        monkeypatch.setattr(
            "treeweft.adapters.postgresql.job_group_store.get_pool", AsyncMock(return_value=None)
        )
        store = JobGroupStore()
        with pytest.raises(RuntimeError):
            await store.latest_by_kind("index-rebuild")


class TestDelete:
    async def test_deletes_exactly_one_row(self, monkeypatch):
        pool, conn = _pool()
        monkeypatch.setattr(
            "treeweft.adapters.postgresql.job_group_store.get_pool", AsyncMock(return_value=pool)
        )
        store = JobGroupStore()
        await store.delete("grp_1")
        query, group_id_arg = conn.execute.call_args.args
        assert "DELETE FROM job_groups WHERE id = $1" in query
        assert group_id_arg == "grp_1"

    async def test_no_pool_raises(self, monkeypatch):
        monkeypatch.setattr(
            "treeweft.adapters.postgresql.job_group_store.get_pool", AsyncMock(return_value=None)
        )
        store = JobGroupStore()
        with pytest.raises(RuntimeError):
            await store.delete("grp_1")


class TestCreateStoresPresetTaskCount:
    async def test_create_passes_task_count_through(self, monkeypatch):
        pool, conn = _pool()
        monkeypatch.setattr(
            "treeweft.adapters.postgresql.job_group_store.get_pool", AsyncMock(return_value=pool)
        )
        store = JobGroupStore()
        await store.create(label="index rebuild", kind="index-rebuild", created_by="admin", task_count=7)
        query, *args = conn.execute.call_args.args
        assert "INSERT INTO job_groups" in query
        # (id, label, kind, created_at, created_by, task_count)
        assert args[-1] == 7
