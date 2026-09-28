"""Startup should warn once when this process's embedding env vars are
ignored because the `embedding_backends` table already has rows.

`EmbeddingBackendStore.seed_from_env_if_empty()` (embedding_backend_store.py)
only writes the table when it is empty; after that the table is
authoritative and the process's EMBEDDING_URLS/EMBEDDING_URL/
EMBEDDING_FALLBACK_URLS/EMBEDDING_FALLBACK_URL are silently ignored. Per the
constitution ("Fail loud, never degrade silently"), lifecycle.py must log a
loud warning in that case via `EmbeddingBackendStore.env_mismatch()` and the
lifecycle-side wrapper `_warn_on_embedding_env_mismatch()`.

No real Postgres: `get_pool()` is monkeypatched to a fake asyncpg pool/conn,
like test_prompt_pin_store.py.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from treeweft.adapters.postgresql import embedding_backend_store as ebs
from treeweft.adapters.postgresql.embedding_backend_store import EmbeddingBackendStore
from treeweft.application import lifecycle

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fake asyncpg pool: an in-memory `embedding_backends` table.
# ---------------------------------------------------------------------------


def _row_dict(url: str, klass: str, enabled: bool = True) -> dict:
    return {
        "id": uuid4(),
        "url": url,
        "klass": klass,
        "enabled": enabled,
        "created_at": datetime.now(UTC),
        "created_by": None,
    }


class _TxCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Backs SELECT COUNT(*), the seeding INSERTs, and the enabled-rows
    SELECT that `list_enabled()`/`env_mismatch()` issue — enough of
    `embedding_backends` to exercise seeding and the mismatch check without
    a real database."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def transaction(self):
        return _TxCtx()

    async def fetchval(self, query: str, *args):
        q = " ".join(query.split())
        if "COUNT(*)" in q:
            return len(self.rows)
        raise AssertionError(f"unexpected fetchval: {q}")

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("NOTIFY"):
            return "NOTIFY"
        if q.startswith("INSERT INTO embedding_backends"):
            (url,) = args
            klass = "gpu" if "'gpu'" in q else "cpu"
            if not any(r["url"] == url for r in self.rows):
                self.rows.append(_row_dict(url, klass))
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {q}")

    async def fetch(self, query: str, *args):
        q = " ".join(query.split())
        if "WHERE enabled = TRUE" in q:
            return [r for r in self.rows if r["enabled"]]
        if q.startswith("SELECT * FROM embedding_backends ORDER BY"):
            return list(self.rows)
        raise AssertionError(f"unexpected fetch: {q}")


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


def _install_fake_pool(monkeypatch, rows: list[dict]) -> _FakeConn:
    conn = _FakeConn(rows)
    pool = _FakePool(conn)

    async def _fake_get_pool():
        return pool

    monkeypatch.setattr(ebs, "get_pool", _fake_get_pool)
    return conn


def _clear_embedding_env(monkeypatch):
    for name in (
        "EMBEDDING_URLS",
        "EMBEDDING_URL",
        "EMBEDDING_FALLBACK_URLS",
        "EMBEDDING_FALLBACK_URL",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# 1. Table rows differ from the environment.
# ---------------------------------------------------------------------------


async def test_gpu_mismatch_logs_one_warning(monkeypatch, caplog):
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_URLS", "http://tei-embedding:80")
    _install_fake_pool(monkeypatch, [_row_dict("http://localhost:8082", "gpu")])

    store = EmbeddingBackendStore()
    with caplog.at_level(logging.WARNING, logger=lifecycle.logger.name):
        await lifecycle._warn_on_embedding_env_mismatch(store)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "http://localhost:8082" in msg
    assert "http://tei-embedding:80" in msg
    assert "/embedding-backends" in msg


# ---------------------------------------------------------------------------
# 2. Table rows equal the environment (any order) -> no warning.
# ---------------------------------------------------------------------------


async def test_matching_sets_log_nothing(monkeypatch, caplog):
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_URLS", "http://b:80,http://a:80")
    _install_fake_pool(
        monkeypatch,
        [_row_dict("http://a:80", "gpu"), _row_dict("http://b:80", "gpu")],
    )

    store = EmbeddingBackendStore()
    with caplog.at_level(logging.WARNING, logger=lifecycle.logger.name):
        await lifecycle._warn_on_embedding_env_mismatch(store)

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# 3. No embedding URL configured at all -> no warning.
# ---------------------------------------------------------------------------


async def test_no_env_urls_logs_nothing(monkeypatch, caplog):
    _clear_embedding_env(monkeypatch)
    _install_fake_pool(monkeypatch, [_row_dict("http://localhost:8082", "gpu")])

    store = EmbeddingBackendStore()
    with caplog.at_level(logging.WARNING, logger=lifecycle.logger.name):
        await lifecycle._warn_on_embedding_env_mismatch(store)

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# 4. EMBEDDING_URLS takes precedence over EMBEDDING_URL, as in seeding.
# ---------------------------------------------------------------------------


async def test_embedding_urls_takes_precedence_over_embedding_url(monkeypatch, caplog):
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_URLS", "http://tei-embedding:80")
    monkeypatch.setenv("EMBEDDING_URL", "http://localhost:8082")
    # Table matches EMBEDDING_URLS, NOT EMBEDDING_URL.
    _install_fake_pool(monkeypatch, [_row_dict("http://tei-embedding:80", "gpu")])

    store = EmbeddingBackendStore()
    with caplog.at_level(logging.WARNING, logger=lifecycle.logger.name):
        await lifecycle._warn_on_embedding_env_mismatch(store)

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# 5. A CPU/fallback difference alone triggers the warning.
# ---------------------------------------------------------------------------


async def test_cpu_fallback_mismatch_alone_triggers_warning(monkeypatch, caplog):
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_URLS", "http://tei-embedding:80")
    monkeypatch.setenv("EMBEDDING_FALLBACK_URLS", "http://tei-cpu:80")
    _install_fake_pool(
        monkeypatch,
        [
            _row_dict("http://tei-embedding:80", "gpu"),
            _row_dict("http://localhost:8083", "cpu"),
        ],
    )

    store = EmbeddingBackendStore()
    with caplog.at_level(logging.WARNING, logger=lifecycle.logger.name):
        await lifecycle._warn_on_embedding_env_mismatch(store)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "http://localhost:8083" in msg
    assert "http://tei-cpu:80" in msg
    assert "/embedding-backends" in msg


async def test_extra_rows_added_through_the_api_log_nothing(monkeypatch, caplog):
    # Every address the environment configures is in the table; the table
    # also holds a backend an operator added with POST /embedding-backends.
    # Nothing from the environment is ignored, so there is nothing to warn
    # about (and warning on every restart would train operators to ignore it).
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_URL", "http://tei-embedding:80")
    _install_fake_pool(
        monkeypatch,
        [
            _row_dict("http://tei-embedding:80", "gpu"),
            _row_dict("http://gpu-box:8082", "gpu"),
            _row_dict("http://tei-cpu:80", "cpu"),
        ],
    )

    store = EmbeddingBackendStore()
    with caplog.at_level(logging.WARNING, logger=lifecycle.logger.name):
        await lifecycle._warn_on_embedding_env_mismatch(store)

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_disabled_row_is_named_with_how_to_fix_it(monkeypatch, caplog):
    # url is UNIQUE, so POSTing an address whose row is disabled fails: the
    # warning must say the row is disabled and to DELETE it first.
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_URL", "http://tei-embedding:80")
    _install_fake_pool(
        monkeypatch, [_row_dict("http://tei-embedding:80", "gpu", enabled=False)]
    )

    store = EmbeddingBackendStore()
    with caplog.at_level(logging.WARNING, logger=lifecycle.logger.name):
        await lifecycle._warn_on_embedding_env_mismatch(store)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "Disabled in the registry: ['http://tei-embedding:80']" in msg
    assert "DELETE a disabled entry" in msg


async def test_trailing_slash_is_not_a_mismatch(monkeypatch, caplog):
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_URL", "http://tei-embedding:80/")
    _install_fake_pool(monkeypatch, [_row_dict("http://tei-embedding:80", "gpu")])

    store = EmbeddingBackendStore()
    with caplog.at_level(logging.WARNING, logger=lifecycle.logger.name):
        await lifecycle._warn_on_embedding_env_mismatch(store)

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# 6. Table was empty and got seeded -> no warning (seeding just made them
#    equal).
# ---------------------------------------------------------------------------


async def test_freshly_seeded_table_logs_nothing(monkeypatch, caplog):
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_URLS", "http://tei-embedding:80")
    monkeypatch.setenv("EMBEDDING_FALLBACK_URLS", "http://tei-cpu:80")
    _install_fake_pool(monkeypatch, [])  # empty table

    store = EmbeddingBackendStore()
    inserted = await store.seed_from_env_if_empty()
    assert inserted  # seeding happened

    with caplog.at_level(logging.WARNING, logger=lifecycle.logger.name):
        # Mirrors lifecycle.startup(): only checked when seeding did NOT
        # happen, but calling it directly here should also find no mismatch
        # since seeding just wrote the env's own URLs into the table.
        await lifecycle._warn_on_embedding_env_mismatch(store)

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# 7. The comparison raising an exception must not stop startup.
# ---------------------------------------------------------------------------


class _ExplodingStore:
    async def env_mismatch(self):
        raise RuntimeError("boom")


async def test_mismatch_exception_is_swallowed_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger=lifecycle.logger.name):
        # Must not raise.
        await lifecycle._warn_on_embedding_env_mismatch(_ExplodingStore())

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not warnings
    assert errors
    assert any(r.exc_info for r in errors)
