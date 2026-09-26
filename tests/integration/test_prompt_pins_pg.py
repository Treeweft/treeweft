"""`prompt_pins` (migration 021) against a real Postgres (ADR-003 §2, T042).

Proves three things the unit tests (`tests/unit/test_prompt_pin_store.py`,
against a fake pool) cannot: that `NOTIFY prompt_pins_changed` actually
reaches a second, independently-listening connection; that the table's
CHECK constraint really is enforced by the server for a HyDE per-source
override; and that `INSERT ... ON CONFLICT DO NOTHING` (the exact statement
`PromptPinStore.seed_if_absent` runs) really does resolve a race between two
connections to exactly one row.

Every row this suite touches uses a scope prefixed `itest-<hex>-...`,
never `'deployment'` — the CHECK constraint's own exemption. `PromptPinStore`
otherwise only knows the app's shared pool (`connection.get_pool()`), so the
NOTIFY test goes through the real store class (via a pool this suite
initializes itself) to prove that code path end-to-end; the CHECK and
conflict-race tests exercise the same SQL directly on raw asyncpg
connections, which is simpler and keeps the assertion tied to the literal
SQL in migration 021 rather than to `PromptPinStore`'s plumbing.

Table creation: only migration 021's `CREATE TABLE IF NOT EXISTS prompt_pins`
statement is applied here (idempotent by the migration file's own design).
The migration's `ALTER TABLE source_records ...` / `UPDATE source_records
SET summary_prompt_version = 3 ...` are deliberately NOT run here — they
touch a table this suite has no scoped/prefixed way to isolate its writes
in, and none of these assertions need those columns. If `prompt_pins`
cannot even be created (e.g. no CREATE privilege on the test DB), the whole
module fails loudly at setup rather than skipping silently.

Run against a standalone instance:

    POSTGRES_TEST_URL=postgresql://user:pass@localhost:5432/treeweft_test \\
      env -u PYTHONPATH python -m pytest tests/integration/test_prompt_pins_pg.py -v

Skipped unless POSTGRES_TEST_URL is set — no service, no silent pass.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

URL = os.environ.get("POSTGRES_TEST_URL")
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not URL, reason="set POSTGRES_TEST_URL to run"),
    pytest.mark.asyncio,
]

NOTIFY_CHANNEL = "prompt_pins_changed"

# migration 021's CREATE TABLE statement, verbatim. See the module
# docstring for why the rest of that migration is not applied here.
_CREATE_PROMPT_PINS = """
CREATE TABLE IF NOT EXISTS prompt_pins (
    operation  TEXT NOT NULL,
    scope      TEXT NOT NULL,
    version    INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT,
    PRIMARY KEY (operation, scope),
    CHECK (operation <> 'hyde' OR scope = 'deployment')
);
"""


@pytest_asyncio.fixture
async def pg_table():
    """Ensures prompt_pins exists (idempotent), on its own short-lived
    connection so the DDL is committed before any test connection opens."""
    conn = await asyncpg.connect(URL)
    try:
        await conn.execute(_CREATE_PROMPT_PINS)
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def scope():
    """A uniquely prefixed, per-test scope. Never 'deployment' — this
    suite must never touch the real deployment pin rows."""
    return f"itest-{uuid.uuid4().hex[:8]}"


async def _delete_scope(operation: str, scope_value: str) -> None:
    conn = await asyncpg.connect(URL)
    try:
        await conn.execute(
            "DELETE FROM prompt_pins WHERE operation = $1 AND scope = $2",
            operation, scope_value,
        )
    finally:
        await conn.close()


class TestNotifyReachesSecondConnection:
    async def test_upsert_notify_reaches_a_listening_connection(self, pg_table, scope):
        """Exercises the real PromptPinStore.upsert() path (via a pool this
        test initializes) — the load-bearing claim is that the NOTIFY it
        sends in the same transaction as the write is actually delivered to
        an independent listening connection, not just that the SQL runs."""
        os.environ["DATABASE_URL"] = URL
        from treeweft.adapters.postgresql import connection
        from treeweft.adapters.postgresql.prompt_pin_store import PromptPinStore

        await connection.init_pool(URL)
        listener_conn = await asyncpg.connect(URL)
        received = asyncio.Event()

        def _on_notify(conn, pid, channel, payload):
            received.set()

        try:
            await listener_conn.add_listener(NOTIFY_CHANNEL, _on_notify)
            try:
                store = PromptPinStore()
                await store.upsert("chunk_summary", scope, 3, "itest-suite")

                await asyncio.wait_for(received.wait(), timeout=5)
                assert received.is_set()
            finally:
                await listener_conn.remove_listener(NOTIFY_CHANNEL, _on_notify)
        finally:
            await listener_conn.close()
            await connection.close_pool()
            await _delete_scope("chunk_summary", scope)


class TestCheckRejectsHydeOverride:
    async def test_check_rejects_non_deployment_hyde_scope(self, pg_table, scope):
        conn = await asyncpg.connect(URL)
        try:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO prompt_pins (operation, scope, version) "
                    "VALUES ($1, $2, $3)",
                    "hyde", scope, 1,
                )
        finally:
            await conn.close()
            # Defensive: the CHECK should have blocked the insert entirely,
            # but clean up in case anything landed.
            await _delete_scope("hyde", scope)


class TestConcurrentSeedRaceLeavesOneRow:
    async def test_concurrent_on_conflict_do_nothing_leaves_one_row(self, pg_table, scope):
        """The exact statement `PromptPinStore.seed_if_absent` runs, fired
        from two genuinely separate connections at once."""
        conn_a = await asyncpg.connect(URL)
        conn_b = await asyncpg.connect(URL)
        sql = (
            "INSERT INTO prompt_pins (operation, scope, version, updated_by) "
            "VALUES ($1, $2, $3, NULL) "
            "ON CONFLICT DO NOTHING"
        )
        try:
            await asyncio.gather(
                conn_a.execute(sql, "chunk_summary", scope, 3),
                conn_b.execute(sql, "chunk_summary", scope, 4),
            )

            check_conn = await asyncpg.connect(URL)
            try:
                rows = await check_conn.fetch(
                    "SELECT version FROM prompt_pins WHERE operation = $1 AND scope = $2",
                    "chunk_summary", scope,
                )
            finally:
                await check_conn.close()

            assert len(rows) == 1
            assert rows[0]["version"] in (3, 4)
        finally:
            await conn_a.close()
            await conn_b.close()
            await _delete_scope("chunk_summary", scope)
