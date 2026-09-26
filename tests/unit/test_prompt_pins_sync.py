"""application.prompt_pins: seeding, fail-loud validation, and process sync

(ADR-003, research R3/R9, tasks T009/T010). Postgres is faked with the
`_FakePool`/`_FakeConn` pattern (tests/unit/test_prompt_pin_store.py,
tests/unit/test_summary_rejection_cache.py:163-186); the dedicated LISTEN
connection is faked separately with `_FakeListenerConn`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytest

from treeweft.adapters.llm_api import prompts
from treeweft.adapters.postgresql import prompt_pin_store as store_mod
from treeweft.adapters.sources import repository as repo_mod
from treeweft.adapters.sources.repository import PostgreSourceRepository
from treeweft.application import indexer_state, prompt_pins

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fake asyncpg pool/conn, shared by the prompt_pins store and the source repo
# ---------------------------------------------------------------------------

class _TxCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.pin_rows: list[dict] = []
        self.source_rows: list[dict] = []
        self.has_table = True
        self.has_version_column = True
        self.has_target_column = True
        self.calls: list[tuple] = []

    def transaction(self):
        return _TxCtx(self)

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split())
        self.calls.append(("fetchrow", q, args))
        if "to_regclass" in q:
            return {
                "has_table": self.has_table,
                "has_version_column": self.has_version_column,
                "has_target_column": self.has_target_column,
            }
        raise AssertionError(f"unexpected fetchrow: {q}")

    async def fetch(self, query: str, *args):
        q = " ".join(query.split())
        self.calls.append(("fetch", q, args))
        if "FROM prompt_pins" in q:
            return [dict(r) for r in self.pin_rows]
        if "summary_prompt_version" in q and "GROUP BY" in q:
            hist: dict[int, int] = {}
            for r in self.source_rows:
                v = r.get("summary_prompt_version")
                if v is not None:
                    hist[v] = hist.get(v, 0) + 1
            return [{"version": v, "n": n} for v, n in hist.items()]
        if "FROM source_records" in q:
            return [dict(r) for r in self.source_rows]
        raise AssertionError(f"unexpected fetch: {q}")

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        self.calls.append(("execute", q, args))
        if q.startswith("NOTIFY"):
            return "NOTIFY"
        if q.startswith("INSERT INTO prompt_pins") and "ON CONFLICT DO NOTHING" in q:
            operation, version = args
            existing = next(
                (r for r in self.pin_rows if r["operation"] == operation and r["scope"] == "deployment"),
                None,
            )
            if existing is not None:
                return "INSERT 0 0"
            self.pin_rows.append(
                {
                    "operation": operation,
                    "scope": "deployment",
                    "version": version,
                    "updated_at": datetime.now(timezone.utc),
                    "updated_by": None,
                }
            )
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {q}")


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self.conn = conn

    def acquire(self):
        return _AcquireCtx(self.conn)


def _pin_row(operation: str, scope: str, version: int) -> dict:
    return {
        "operation": operation,
        "scope": scope,
        "version": version,
        "updated_at": datetime.now(timezone.utc),
        "updated_by": None,
    }


def _source_row(version: int | None) -> dict:
    return {
        "id": f"src-{version}",
        "path": "/repo",
        "url": "",
        "branch": "",
        "indexed_at": datetime.now(timezone.utc),
        "file_count": 1,
        "chunk_count": 1,
        "summary_prompt_version": version,
    }


@pytest.fixture
def conn():
    return _FakeConn()


@pytest.fixture
def wired(monkeypatch, conn):
    """Point the prompt_pins module, the store and the source repo at one fake pool."""
    pool = _FakePool(conn)

    async def _get_pool():
        return pool

    monkeypatch.setattr(store_mod, "get_pool", _get_pool)
    monkeypatch.setattr(prompt_pins, "get_pool", _get_pool)
    monkeypatch.setattr(prompt_pins, "_source_repo", PostgreSourceRepository(pool=pool))
    monkeypatch.setattr(indexer_state, "DATABASE_URL", "postgresql://fake/db")
    return conn


@pytest.fixture(autouse=True)
def _reset_view():
    prompt_pins._view = prompt_pins.PinView(deployment=dict(prompts.BASELINE), overrides={})
    yield
    prompt_pins._view = prompt_pins.PinView(deployment=dict(prompts.BASELINE), overrides={})


# ---------------------------------------------------------------------------
# load_and_seed: seeding rules
# ---------------------------------------------------------------------------

class TestSeeding:
    async def test_seeds_latest_with_empty_table_and_no_sources(self, wired):
        await prompt_pins.load_and_seed()

        view = prompt_pins.view()
        assert view.deployment["chunk_summary"] == prompts.latest("chunk_summary").version
        assert view.deployment["hyde"] == prompts.latest("hyde").version

    async def test_seeds_from_histogram_with_v3_sources(self, wired):
        wired.source_rows = [_source_row(3), _source_row(3), _source_row(None)]

        await prompt_pins.load_and_seed()

        view = prompt_pins.view()
        assert view.deployment["chunk_summary"] == 3
        assert view.deployment["hyde"] == 1

    async def test_logs_each_seed(self, wired, caplog):
        caplog.set_level(logging.INFO, logger="treeweft.application.prompt_pins")

        await prompt_pins.load_and_seed()

        messages = [r.getMessage() for r in caplog.records]
        assert any("seeded prompt pin chunk_summary=3" in m for m in messages)
        assert any("seeded prompt pin hyde=1" in m for m in messages)

    async def test_concurrent_load_and_seed_leaves_one_row_per_operation(self, wired, conn):
        await asyncio.gather(prompt_pins.load_and_seed(), prompt_pins.load_and_seed())

        deployment_rows = [r for r in conn.pin_rows if r["scope"] == "deployment"]
        ops = [r["operation"] for r in deployment_rows]
        assert sorted(ops) == sorted(set(ops))
        assert set(ops) == {"chunk_summary", "hyde"}


# ---------------------------------------------------------------------------
# load_and_seed: fail-loud validation
# ---------------------------------------------------------------------------

class TestFailLoud:
    async def test_missing_table_raises_mentioning_migration_021(self, wired):
        wired.has_table = False

        with pytest.raises(RuntimeError, match="migration 021"):
            await prompt_pins.load_and_seed()

    async def test_missing_column_raises_mentioning_migration_021(self, wired):
        wired.has_version_column = False

        with pytest.raises(RuntimeError, match="migration 021"):
            await prompt_pins.load_and_seed()

    async def test_unregistered_stored_deployment_pin_raises(self, wired, monkeypatch):
        extra = prompts.PromptVersion(
            operation="chunk_summary",
            version=4,
            system="test v4",
            schema=prompts.FrozenResponseSchema(min_length=1),
            notes="test-only v4",
        )
        monkeypatch.setitem(
            prompts.REGISTRY, "chunk_summary", {3: prompts.REGISTRY["chunk_summary"][3], 4: extra}
        )
        wired.pin_rows = [
            _pin_row("chunk_summary", "deployment", 3),
            _pin_row("hyde", "deployment", 1),
            _pin_row("chunk_summary", "src-1", 99),
        ]

        with pytest.raises(RuntimeError) as exc_info:
            await prompt_pins.load_and_seed()

        msg = str(exc_info.value)
        assert "chunk_summary" in msg
        assert "src-1" in msg
        assert "99" in msg
        assert "3" in msg
        assert "4" in msg


# ---------------------------------------------------------------------------
# No DATABASE_URL: baseline forever, one warning, no writes
# ---------------------------------------------------------------------------

class TestNoDatabase:
    async def test_effective_returns_baseline(self, monkeypatch):
        monkeypatch.setattr(indexer_state, "DATABASE_URL", "")

        assert prompt_pins.effective("chunk_summary") == prompts.BASELINE["chunk_summary"]
        assert prompt_pins.effective("hyde") == prompts.BASELINE["hyde"]

    async def test_load_and_seed_writes_nothing_and_warns_once(self, monkeypatch, caplog):
        monkeypatch.setattr(indexer_state, "DATABASE_URL", "")

        class _BoomStore:
            def __getattr__(self, name):
                raise AssertionError(f"prompt pin store should not be touched: {name}")

        monkeypatch.setattr(prompt_pins, "_pin_store", _BoomStore())
        caplog.set_level(logging.WARNING, logger="treeweft.application.prompt_pins")

        await prompt_pins.load_and_seed()

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings.count(
            "prompt pins unavailable without DATABASE_URL; using baseline chunk_summary v3, hyde v1"
        ) == 1


# ---------------------------------------------------------------------------
# reload(): HyDE cache invalidation
# ---------------------------------------------------------------------------

class TestReloadHydeCache:
    async def test_hyde_pin_change_clears_cache(self, wired, monkeypatch):
        from treeweft.adapters.llm_api.llm_adapter import _HYDE_CACHE

        hyde_v1 = prompts.REGISTRY["hyde"][1]
        hyde_v2 = prompts.PromptVersion(
            operation="hyde", version=2, system="test v2",
            schema=prompts.FrozenResponseSchema(min_length=1), notes="test-only v2",
        )
        monkeypatch.setitem(prompts.REGISTRY, "hyde", {1: hyde_v1, 2: hyde_v2})

        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 1)]
        await prompt_pins.load_and_seed()

        _HYDE_CACHE["k"] = "v"
        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 2)]
        await prompt_pins.reload()

        assert prompt_pins.view().deployment["hyde"] == 2
        assert _HYDE_CACHE == {}

    async def test_hyde_pin_unchanged_does_not_clear_cache(self, wired):
        from treeweft.adapters.llm_api.llm_adapter import _HYDE_CACHE

        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 1)]
        await prompt_pins.load_and_seed()

        _HYDE_CACHE["k"] = "v"
        await prompt_pins.reload()

        assert _HYDE_CACHE == {"k": "v"}
        _HYDE_CACHE.clear()


# ---------------------------------------------------------------------------
# reload(): an unregistered stored pin logs and keeps the previous view
# ---------------------------------------------------------------------------

class TestReloadDoesNotCrash:
    async def test_reload_with_unregistered_version_keeps_previous_view(self, wired, caplog):
        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 1)]
        await prompt_pins.load_and_seed()
        before = prompt_pins.view()

        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 12345), _pin_row("hyde", "deployment", 1)]
        caplog.set_level(logging.ERROR, logger="treeweft.application.prompt_pins")

        await prompt_pins.reload()

        assert prompt_pins.view() == before
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    async def test_reload_missing_deployment_pin_keeps_previous_view(self, wired, caplog):
        """A hand-deleted deployment row for a still-registered operation
        must not install a view that would KeyError out of `effective()`."""
        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 1)]
        await prompt_pins.load_and_seed()
        before = prompt_pins.view()

        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 3)]  # hyde row gone
        caplog.set_level(logging.ERROR, logger="treeweft.application.prompt_pins")

        await prompt_pins.reload()

        assert prompt_pins.view() == before
        assert prompt_pins.effective("hyde") == 1  # still resolvable, no KeyError
        error_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("hyde" in m for m in error_messages)


# ---------------------------------------------------------------------------
# reload(): concurrent calls are serialized so an older, slower read can
# never complete after a newer one and clobber the fresher view.
# ---------------------------------------------------------------------------

def _pin_row_obj(operation: str, scope: str, version: int) -> "store_mod.PinRow":
    return store_mod.PinRow(
        operation=operation, scope=scope, version=version,
        updated_at=datetime.now(timezone.utc), updated_by=None,
    )


class TestReloadSerialization:
    async def test_out_of_order_reload_completion_keeps_newest(self, wired, monkeypatch):
        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 1)]
        await prompt_pins.load_and_seed()

        monkeypatch.setitem(
            prompts.REGISTRY,
            "chunk_summary",
            {
                3: prompts.REGISTRY["chunk_summary"][3],
                4: prompts.PromptVersion(
                    operation="chunk_summary", version=4, system="v4",
                    schema=prompts.FrozenResponseSchema(min_length=1), notes="test v4",
                ),
            },
        )

        calls = {"n": 0}

        async def _slow_then_fast(*_a, **_kw):
            calls["n"] += 1
            n = calls["n"]
            if n == 1:
                # The first caller reads a stale snapshot (still v3) but is
                # slow to return it -- e.g. a laggy connection.
                await asyncio.sleep(0.05)
                return [_pin_row_obj("chunk_summary", "deployment", 3), _pin_row_obj("hyde", "deployment", 1)]
            # The second caller reads the fresher state and returns fast.
            return [_pin_row_obj("chunk_summary", "deployment", 4), _pin_row_obj("hyde", "deployment", 1)]

        monkeypatch.setattr(prompt_pins._pin_store, "list_all", _slow_then_fast)

        t_old = asyncio.create_task(prompt_pins.reload())
        await asyncio.sleep(0)  # let t_old start its (slow) fetch first
        t_new = asyncio.create_task(prompt_pins.reload())
        await asyncio.gather(t_old, t_new)

        assert prompt_pins.view().deployment["chunk_summary"] == 4


# ---------------------------------------------------------------------------
# NOTIFY callback: the reload() task it fires must not be garbage-collected
# before it completes.
# ---------------------------------------------------------------------------

class TestNotifyTaskReferenceHeld:
    async def test_notify_callback_task_is_tracked_until_done(self, wired, monkeypatch):
        import asyncpg as asyncpg_mod

        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 1)]
        await prompt_pins.load_and_seed()

        fake_conn = _FakeListenerConn()

        async def _fake_connect(_dsn):
            return fake_conn

        monkeypatch.setattr(asyncpg_mod, "connect", _fake_connect)
        monkeypatch.setattr(prompt_pins, "_LISTENER_POLL_SECONDS", 0.01)

        prompt_pins._stopping = False
        task = asyncio.create_task(prompt_pins._listener_supervisor("postgresql://fake/db"))
        try:
            for _ in range(50):
                await asyncio.sleep(0.01)
                if fake_conn.listeners:
                    break
            assert fake_conn.listeners, "listener callback was never registered"
            channel, callback = fake_conn.listeners[0]

            assert prompt_pins._notify_tasks == set()
            callback(fake_conn, 0, channel, "")
            assert len(prompt_pins._notify_tasks) == 1

            for _ in range(50):
                await asyncio.sleep(0.01)
                if not prompt_pins._notify_tasks:
                    break
            assert prompt_pins._notify_tasks == set()
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            await prompt_pins.stop_sync()
            prompt_pins._stopping = False


# ---------------------------------------------------------------------------
# Listener supervisor: reload after a simulated reconnect
# ---------------------------------------------------------------------------

class _FakeListenerConn:
    def __init__(self):
        self._closed = False
        self.listeners: list[tuple] = []

    async def add_listener(self, channel, callback):
        self.listeners.append((channel, callback))

    def is_closed(self):
        return self._closed

    async def close(self):
        self._closed = True


class TestListenerReconnect:
    async def test_reload_after_simulated_reconnect_picks_up_change(self, wired, monkeypatch):
        import asyncpg as asyncpg_mod

        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 1)]
        await prompt_pins.load_and_seed()

        monkeypatch.setattr(prompt_pins, "_LISTENER_BACKOFF_INITIAL", 0.01)
        monkeypatch.setattr(prompt_pins, "_LISTENER_POLL_SECONDS", 0.01)

        attempts = {"n": 0}

        async def _fake_connect(dsn):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("simulated drop")
            return _FakeListenerConn()

        monkeypatch.setattr(asyncpg_mod, "connect", _fake_connect)

        # Change made "while disconnected" — the first connect attempt fails.
        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 1)]

        task = asyncio.create_task(prompt_pins._listener_supervisor("postgresql://fake/db"))
        try:
            wired.pin_rows = [_pin_row("chunk_summary", "deployment", 4), _pin_row("hyde", "deployment", 1)]
            monkeypatch.setitem(
                prompts.REGISTRY,
                "chunk_summary",
                {
                    3: prompts.REGISTRY["chunk_summary"][3],
                    4: prompts.PromptVersion(
                        operation="chunk_summary", version=4, system="v4",
                        schema=prompts.FrozenResponseSchema(min_length=1), notes="test v4",
                    ),
                },
            )

            for _ in range(50):
                await asyncio.sleep(0.02)
                if prompt_pins.view().deployment.get("chunk_summary") == 4:
                    break

            assert prompt_pins.view().deployment["chunk_summary"] == 4
            assert attempts["n"] >= 2
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            await prompt_pins.stop_sync()


# ---------------------------------------------------------------------------
# Periodic reload
# ---------------------------------------------------------------------------

class TestPeriodicReload:
    async def test_periodic_reload_picks_up_change(self, wired, monkeypatch):
        wired.pin_rows = [_pin_row("chunk_summary", "deployment", 3), _pin_row("hyde", "deployment", 1)]
        await prompt_pins.load_and_seed()

        monkeypatch.setattr(prompt_pins, "prompt_pins_refresh_seconds", lambda: 0.01)

        prompt_pins._stopping = False
        task = asyncio.create_task(prompt_pins._periodic_reload_loop())
        try:
            monkeypatch.setitem(
                prompts.REGISTRY,
                "chunk_summary",
                {
                    3: prompts.REGISTRY["chunk_summary"][3],
                    4: prompts.PromptVersion(
                        operation="chunk_summary", version=4, system="v4",
                        schema=prompts.FrozenResponseSchema(min_length=1), notes="test v4",
                    ),
                },
            )
            wired.pin_rows = [_pin_row("chunk_summary", "deployment", 4), _pin_row("hyde", "deployment", 1)]

            for _ in range(50):
                await asyncio.sleep(0.02)
                if prompt_pins.view().deployment.get("chunk_summary") == 4:
                    break

            assert prompt_pins.view().deployment["chunk_summary"] == 4
        finally:
            prompt_pins._stopping = True
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
