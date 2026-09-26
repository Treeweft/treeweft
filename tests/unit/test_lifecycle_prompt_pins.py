"""`lifecycle.startup()`/`shutdown()` wiring for prompt pins (ADR-003, T018).

Research R9: `prompt_pins.load_and_seed()` runs after `run_migrations()` and
before the index-schema check and the job queue starts, with no
try/except (constitution V) — a schema or validation failure must abort
startup, not be logged and swallowed like the graph-schema and
index-guard steps around it. `start_sync()` follows it, and `stop_sync()`
runs in `shutdown()`.

Running the real `startup()` needs Postgres, migrations and an embedding
backend, so every heavy dependency is faked: a truthy `DATABASE_URL` drives
the code down the branches that matter, `EMBEDDING_PROVIDER=openai` skips
the embedding-proxy block, and the job store/group store/queue classes are
replaced with fakes that record their call into a shared `order` list — the
same recorded-call-order approach as `test_lifecycle_order.py`, but
executed rather than parsed, since a RuntimeError's propagation can only be
observed by running the code.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from treeweft.application import index_guard
from treeweft.application import indexer_state as _state
from treeweft.application import lifecycle


@pytest.fixture(autouse=True)
def _restore_job_globals():
    """`startup()` assigns `_state._job_store`/`_job_group_store`/`_job_queue`
    directly (not through anything `monkeypatch` tracks), so running it here
    against fakes would otherwise leak those fakes into every later test in
    the session — `monkeypatch`'s teardown only reverts the attributes it
    itself patched (the `JobStore`/`JobGroupStore`/`PostgresJobQueue`
    classes), not this module-level state the code assigns at runtime."""
    job_store = _state._job_store
    job_group_store = _state._job_group_store
    job_queue = _state._job_queue
    yield
    _state._job_store = job_store
    _state._job_group_store = job_group_store
    _state._job_queue = job_queue


class _FakeUserStore:
    async def count_users(self):
        return 1  # non-zero: _seed_admin_if_first_start() returns immediately


class _FakeJobStore:
    def __init__(self, order):
        self._order = order

    async def init(self):
        self._order.append("job_store.init")

    async def list_by_status(self, status):
        return []


class _FakeJobGroupStore:
    def __init__(self, order):
        self._order = order

    async def init(self):
        self._order.append("job_group_store.init")


class _FakeQueue:
    def __init__(self, order):
        self._order = order

    async def start(self):
        self._order.append("queue.start")

    async def stop(self):
        pass

    async def enqueue(self, job_id):
        pass


def _patch_common(monkeypatch, order, *, load_and_seed=None, start_sync=None):
    """Fake every heavy dependency of `startup()`, wiring the DATABASE_URL
    branch so `run_migrations`, the job stores and the queue all run (as
    fakes), while the embedding-proxy block is skipped entirely."""
    app = SimpleNamespace(state=SimpleNamespace())

    monkeypatch.setattr(lifecycle, "_LOGIN_PURGE_ENABLED", False)
    monkeypatch.setattr(lifecycle, "_FRESHNESS_SAMPLER_ENABLED", False)
    monkeypatch.setattr(_state, "DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setattr(_state, "_user_store", _FakeUserStore())
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")

    monkeypatch.setattr("treeweft.infrastructure.config.validate_config", MagicMock())
    monkeypatch.setattr("treeweft.infrastructure.logging.configure_logging", MagicMock())
    monkeypatch.setattr("treeweft.infrastructure.tracing.init_tracer", MagicMock())

    async def fake_init_pool(dsn):
        return object()

    monkeypatch.setattr("treeweft.adapters.postgresql.connection.init_pool", fake_init_pool)

    async def fake_run_migrations():
        order.append("run_migrations")
        return []

    monkeypatch.setattr("treeweft.adapters.postgresql.run_migrations", fake_run_migrations)

    async def fake_ensure_schema():
        order.append("graph_store.ensure_schema")

    monkeypatch.setattr(lifecycle.graph_store, "ensure_schema", fake_ensure_schema)

    monkeypatch.setattr(
        "treeweft.adapters.postgresql.job_store.JobStore", lambda: _FakeJobStore(order)
    )
    monkeypatch.setattr(
        "treeweft.adapters.postgresql.job_group_store.JobGroupStore",
        lambda: _FakeJobGroupStore(order),
    )
    monkeypatch.setattr(
        "treeweft.adapters.queue.postgres_queue.PostgresJobQueue", lambda: _FakeQueue(order)
    )

    async def fake_run_check():
        order.append("index_guard.run_check")

    monkeypatch.setattr(lifecycle.index_guard, "run_check", fake_run_check)
    monkeypatch.setattr(lifecycle.index_guard, "start_refresh_loop", MagicMock())

    async def default_load_and_seed():
        order.append("load_and_seed")

    async def default_start_sync():
        order.append("start_sync")

    monkeypatch.setattr(
        lifecycle.prompt_pins, "load_and_seed", load_and_seed or default_load_and_seed
    )
    monkeypatch.setattr(lifecycle.prompt_pins, "start_sync", start_sync or default_start_sync)

    return app


class TestStartupOrder:
    def test_load_and_seed_runs_after_migrations_and_before_guard_and_queue(self, monkeypatch):
        order: list[str] = []
        app = _patch_common(monkeypatch, order)

        asyncio.run(lifecycle.startup(app))

        assert order.index("run_migrations") < order.index("load_and_seed")
        assert order.index("load_and_seed") < order.index("index_guard.run_check")
        assert order.index("load_and_seed") < order.index("queue.start")

    def test_start_sync_runs_after_seeding(self, monkeypatch):
        order: list[str] = []
        app = _patch_common(monkeypatch, order)

        asyncio.run(lifecycle.startup(app))

        assert order.index("load_and_seed") < order.index("start_sync")


class TestStartupFailsLoud:
    def test_runtime_error_from_load_and_seed_propagates_and_aborts_startup(self, monkeypatch):
        order: list[str] = []

        async def boom():
            order.append("load_and_seed")
            raise RuntimeError("prompt_pins table or source_records summary columns are missing")

        app = _patch_common(monkeypatch, order, load_and_seed=boom)

        with pytest.raises(RuntimeError, match="prompt_pins table"):
            asyncio.run(lifecycle.startup(app))

        # Not logged-and-swallowed: nothing downstream of the seed ran.
        assert "start_sync" not in order
        assert "index_guard.run_check" not in order
        assert "queue.start" not in order


class TestShutdownStopsSync:
    def test_shutdown_calls_stop_sync(self, monkeypatch):
        called: list[str] = []

        async def fake_stop_sync():
            called.append("stop_sync")

        monkeypatch.setattr(lifecycle.prompt_pins, "stop_sync", fake_stop_sync)
        monkeypatch.setattr(index_guard, "stop_refresh_loop", AsyncMock())
        monkeypatch.setattr(lifecycle.graph_store, "close", AsyncMock())
        monkeypatch.setattr(_state, "_job_queue", None)
        monkeypatch.setattr(_state, "_freshness_sampler_task", None)
        monkeypatch.setattr(_state, "_login_purge_task", None)
        monkeypatch.setattr(_state, "_fleet_refresh_task", None)
        monkeypatch.setattr(
            "treeweft.adapters.postgresql.connection.close_pool", AsyncMock()
        )

        app = SimpleNamespace(state=SimpleNamespace())
        asyncio.run(lifecycle.shutdown(app))

        assert called == ["stop_sync"]
