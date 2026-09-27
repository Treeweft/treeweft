"""`lifecycle.startup()` aborts when a migration fails.

Constitution V: `run_migrations()` raises `MigrationError` and `startup()`
must let it through — no try/except around the call, unlike the
graph-schema and index-guard steps after it. Otherwise the indexer serves
requests on a partial schema and the failure shows up later, far from the
cause, in whatever code path first touches the missing table or column.

Executed rather than parsed, with every heavy dependency faked by the
harness in test_lifecycle_prompt_pins.py: a raised error's propagation can
only be observed by running the code.
"""
from __future__ import annotations

import asyncio

import pytest

from treeweft.adapters.postgresql import MigrationError
from treeweft.application import lifecycle
from tests.unit.test_lifecycle_prompt_pins import (  # noqa: F401  (fixture)
    _patch_common,
    _restore_job_globals,
)


class TestStartupFailsLoud:
    def test_migration_error_propagates_and_aborts_startup(self, monkeypatch):
        order: list[str] = []
        app = _patch_common(monkeypatch, order)

        async def boom():
            order.append("run_migrations")
            raise MigrationError(
                'Migration 021_prompt_versions.sql failed: UndefinedTableError: '
                'relation "source_records" does not exist'
            )

        monkeypatch.setattr("treeweft.adapters.postgresql.run_migrations", boom)

        with pytest.raises(MigrationError, match="021_prompt_versions.sql"):
            asyncio.run(lifecycle.startup(app))

        # Not logged-and-swallowed: nothing downstream of the migrations ran.
        assert order == ["run_migrations"]
