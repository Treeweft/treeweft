"""PostgreSQL adapter package for treeweft.

Provides connection pooling and migration support. All database interaction
goes through asyncpg pools managed by connection.py.
"""

import logging
import os
from pathlib import Path

from treeweft.adapters.postgresql.connection import init_pool, get_pool, close_pool  # noqa: F401

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATIONS_TABLE = "treeweft_migrations"

# Serializes indexer processes that start together: the second waits for the
# first's transaction, then finds its tracking row and skips. Same two-key
# hashtext() form as maintenance_lock._LOCK_KEY_EXPR, different second key.
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('treeweft'), hashtext('migrations'))"


class MigrationError(RuntimeError):
    """A migration could not be applied. Indexer startup must not continue:
    the schema is behind what the code expects (constitution V)."""


def _failed(what: str, exc: BaseException) -> MigrationError:
    return MigrationError(f"{what} failed: {type(exc).__name__}: {exc}")


async def run_migrations() -> list[str]:
    """Run pending SQL migrations against the PostgreSQL database.

    Creates a tracking table (treeweft_migrations) to ensure each migration
    runs exactly once. Migration files are loaded in name order from the
    migrations/ directory.

    Each migration runs in one transaction together with its tracking row,
    so either both are committed or neither is. Migration files must not
    contain BEGIN/COMMIT/ROLLBACK of their own. A statement that cannot run
    inside a transaction (CREATE INDEX CONCURRENTLY) is not supported.

    Returns:
        List of migration filenames that were applied. Empty list if no
        migrations were pending or the pool is unavailable.

    Raises:
        MigrationError: a migration failed. The message names the file and
            the database error. No later migration was attempted; the failed
            one was rolled back, so the next run retries it.
    """
    pool = await get_pool()
    if pool is None:
        logger.warning("PostgreSQL pool unavailable — migrations skipped")
        return []

    applied: list[str] = []

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute(_LOCK_SQL)
                await conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {_MIGRATIONS_TABLE} (name TEXT PRIMARY KEY)"
                )
            rows = await conn.fetch(f"SELECT name FROM {_MIGRATIONS_TABLE}")
        except Exception as exc:
            raise _failed(f"Preparing the {_MIGRATIONS_TABLE} table", exc) from exc
        done = {row["name"] for row in rows}

        migration_files = sorted(
            f for f in os.listdir(_MIGRATIONS_DIR) if f.endswith(".sql")
        )
        for fname in migration_files:
            if fname in done:
                continue  # Already applied

            try:
                async with conn.transaction():
                    await conn.execute(_LOCK_SQL)
                    # Re-check under the lock: another indexer process may
                    # have applied it since `done` was read.
                    record = await conn.fetchrow(
                        f"SELECT name FROM {_MIGRATIONS_TABLE} WHERE name = $1", fname
                    )
                    if record is not None:
                        continue

                    sql = (_MIGRATIONS_DIR / fname).read_text()
                    await conn.execute(sql)
                    if not conn.is_in_transaction():
                        raise MigrationError(
                            f"Migration {fname} ended the transaction it runs in, so "
                            "its changes were committed without a tracking row. "
                            "Remove BEGIN/COMMIT/ROLLBACK from the file."
                        )
                    await conn.execute(
                        f"INSERT INTO {_MIGRATIONS_TABLE} (name) VALUES ($1)", fname
                    )
            except MigrationError:
                raise
            except Exception as exc:
                raise _failed(f"Migration {fname}", exc) from exc

            logger.info("Applied migration: %s", fname)
            applied.append(fname)

    return applied
