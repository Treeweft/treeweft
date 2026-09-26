"""Postgres-backed storage for prompt version pins (ADR-003, migration 021).

`prompt_pins` holds the admin-controlled pin per operation: a 'deployment'
row for the whole deployment, or a source_id row for a chunk_summary
per-source override (hyde has no per-source override; see the table's
CHECK constraint). Mutating methods emit `NOTIFY prompt_pins_changed` in
the same transaction as the write, so a rollback discards the notification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from treeweft.adapters.postgresql.connection import get_pool

logger = logging.getLogger(__name__)

NOTIFY_CHANNEL = "prompt_pins_changed"

_POOL_REQUIRED = (
    "PromptPinStore requires DATABASE_URL to be set and reachable. "
    "Set DATABASE_URL=postgresql://... in your environment."
)


@dataclass
class PinRow:
    operation: str
    scope: str
    version: int
    updated_at: datetime
    updated_by: str | None

    def to_dict(self) -> dict:
        return {
            "operation": self.operation,
            "scope": self.scope,
            "version": self.version,
            "updated_at": self.updated_at.isoformat(),
            "updated_by": self.updated_by,
        }


def _row_to_pin(row) -> PinRow:
    return PinRow(
        operation=row["operation"],
        scope=row["scope"],
        version=row["version"],
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
    )


class PromptPinStore:
    """Async CRUD over the `prompt_pins` table."""

    async def list_all(self) -> list[PinRow]:
        pool = await get_pool()
        if pool is None:
            raise RuntimeError(_POOL_REQUIRED)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM prompt_pins ORDER BY operation, scope"
            )
        return [_row_to_pin(r) for r in rows]

    async def upsert(
        self, operation: str, scope: str, version: int, updated_by: str | None
    ) -> PinRow:
        pool = await get_pool()
        if pool is None:
            raise RuntimeError(_POOL_REQUIRED)
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO prompt_pins (operation, scope, version, updated_by)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (operation, scope) DO UPDATE SET
                        version    = EXCLUDED.version,
                        updated_at = NOW(),
                        updated_by = EXCLUDED.updated_by
                    RETURNING *
                    """,
                    operation, scope, version, updated_by,
                )
                # NOTIFY in the same transaction as the write — rollback
                # of the txn discards the notification.
                await conn.execute(f"NOTIFY {NOTIFY_CHANNEL}")
        return _row_to_pin(row)

    async def delete(self, operation: str, scope: str) -> bool:
        pool = await get_pool()
        if pool is None:
            raise RuntimeError(_POOL_REQUIRED)
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    "DELETE FROM prompt_pins WHERE operation = $1 AND scope = $2",
                    operation, scope,
                )
                deleted = result.endswith(" 1")
                if deleted:
                    await conn.execute(f"NOTIFY {NOTIFY_CHANNEL}")
        return deleted

    async def seed_if_absent(self, operation: str, version: int) -> None:
        """Insert the deployment pin for `operation` only if none exists yet.

        Startup seeding: never overwrites an admin-set pin. NOTIFYs only
        when a row was actually inserted, since nothing is listening at
        the very first seed and a redundant NOTIFY on every restart is
        just noise for later ones.
        """
        pool = await get_pool()
        if pool is None:
            raise RuntimeError(_POOL_REQUIRED)
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """
                    INSERT INTO prompt_pins (operation, scope, version, updated_by)
                    VALUES ($1, 'deployment', $2, NULL)
                    ON CONFLICT DO NOTHING
                    """,
                    operation, version,
                )
                inserted = result.endswith(" 1")
                if inserted:
                    await conn.execute(f"NOTIFY {NOTIFY_CHANNEL}")

    async def delete_overrides_for_source(self, source_id: str) -> int:
        """Delete every per-source override for `source_id`.

        Never touches the 'deployment' scope, even if a caller somehow
        passes that literal string as a source_id.
        """
        pool = await get_pool()
        if pool is None:
            raise RuntimeError(_POOL_REQUIRED)
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    "DELETE FROM prompt_pins WHERE scope = $1 AND scope <> 'deployment'",
                    source_id,
                )
                deleted = int(result.rsplit(" ", 1)[-1]) if result else 0
                if deleted:
                    await conn.execute(f"NOTIFY {NOTIFY_CHANNEL}")
        return deleted
