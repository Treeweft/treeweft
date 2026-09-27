"""PostgreSQL adapter package for treeweft.

Provides connection pooling and migration support. All database interaction
goes through asyncpg pools managed by connection.py.
"""

import logging
import os
import re
from pathlib import Path

from treeweft.adapters.postgresql.connection import init_pool, get_pool, close_pool  # noqa: F401

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATIONS_TABLE = "treeweft_migrations"

# Serializes indexer processes that start together: the second waits for the
# first's transaction, then finds its tracking row and skips. Same two-key
# hashtext() form as maintenance_lock._LOCK_KEY_EXPR, different second key.
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('treeweft'), hashtext('migrations'))"

# The pool's command_timeout (10s) suits request-path queries, not a backfill
# or index build on a large table. A timeout here stops startup, and every
# restart would redo the same work and time out again. The wait for the lock
# gets the same limit: it lasts as long as another process's migration.
_MIGRATION_TIMEOUT_SECONDS = 3600.0

# READ COMMITTED whatever the database default is: under REPEATABLE READ the
# snapshot predates the wait for the lock, so the re-check would not see the
# row the lock's previous holder just committed.
_ISOLATION = "read_committed"

_TRANSACTION_CONTROL = re.compile(
    r"(BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK|ABORT|PREPARE\s+TRANSACTION)\b",
    re.IGNORECASE,
)


class MigrationError(RuntimeError):
    """A migration could not be applied. Indexer startup must not continue:
    the schema is behind what the code expects (constitution V)."""


def _failed(what: str, exc: BaseException) -> MigrationError:
    detail = str(exc)
    if not detail and isinstance(exc, TimeoutError):
        detail = "a statement, or the wait for the migrations lock, exceeded its time limit"
    return MigrationError(f"{what} failed: {type(exc).__name__}: {detail}")


def _migration_files() -> list[str]:
    return sorted(f for f in os.listdir(_MIGRATIONS_DIR) if f.endswith(".sql"))


def _statements(sql: str) -> list[str]:
    """Split a script into statements, blanking comments, quoted text and
    dollar-quoted bodies so that a `;` or a keyword inside them is not read
    as SQL."""
    statements: list[str] = []
    current: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            current.append(" ")
        elif ch in ("'", '"'):
            i += 1
            while i < n:
                if sql[i] == ch:
                    if sql.startswith(ch, i + 1):  # doubled quote: escaped
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            current.append(" ")
        elif ch == "$" and (tag := re.match(r"\$\w*\$", sql[i:])):
            end = sql.find(tag.group(), i + tag.end())
            i = n if end == -1 else end + tag.end()
            current.append(" ")
        elif ch == ";":
            statements.append("".join(current).strip())
            current = []
            i += 1
        else:
            current.append(ch)
            i += 1
    statements.append("".join(current).strip())
    return [s for s in statements if s]


def _transaction_control(sql: str) -> list[str]:
    """The statements in a migration script that start or end a transaction."""
    return [s for s in _statements(sql) if _TRANSACTION_CONTROL.match(s)]


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
            async with conn.transaction(isolation=_ISOLATION):
                await conn.execute(_LOCK_SQL, timeout=_MIGRATION_TIMEOUT_SECONDS)
                # Not CREATE TABLE IF NOT EXISTS: that needs CREATE on the
                # schema even when the table exists, which a role with
                # read/write rights only does not have.
                exists = await conn.fetchval(
                    "SELECT to_regclass($1) IS NOT NULL", _MIGRATIONS_TABLE
                )
                if not exists:
                    await conn.execute(
                        f"CREATE TABLE {_MIGRATIONS_TABLE} (name TEXT PRIMARY KEY)"
                    )
            rows = await conn.fetch(f"SELECT name FROM {_MIGRATIONS_TABLE}")
        except Exception as exc:
            raise _failed(f"Preparing the {_MIGRATIONS_TABLE} table", exc) from exc
        done = {row["name"] for row in rows}

        for fname in _migration_files():
            if fname in done:
                continue  # Already applied

            try:
                sql = (_MIGRATIONS_DIR / fname).read_text()
                found = _transaction_control(sql)
                if found:
                    raise MigrationError(
                        f"Migration {fname} contains transaction control "
                        f"({'; '.join(found)}). Remove it: every migration already "
                        "runs in one transaction with its tracking row. Nothing "
                        "in the file was run."
                    )

                async with conn.transaction(isolation=_ISOLATION):
                    await conn.execute(_LOCK_SQL, timeout=_MIGRATION_TIMEOUT_SECONDS)
                    # Re-check under the lock: another indexer process may
                    # have applied it since `done` was read.
                    record = await conn.fetchrow(
                        f"SELECT name FROM {_MIGRATIONS_TABLE} WHERE name = $1", fname
                    )
                    if record is not None:
                        continue

                    await conn.execute(sql, timeout=_MIGRATION_TIMEOUT_SECONDS)
                    # Backstop for what _transaction_control() cannot see,
                    # such as a COMMIT inside a procedure the script calls.
                    if not conn.is_in_transaction():
                        raise MigrationError(
                            f"Migration {fname} ended the transaction it runs in. "
                            "It has no tracking row, and whatever it committed "
                            "before that point is still in the database."
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
