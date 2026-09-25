"""Postgres advisory maintenance lock (ADR-004 §3, research R13).

Coordinates a rebuild, a community-build backfill, and index-stamp writes
across several indexer processes sharing one Postgres. The lock is held on
a DEDICATED connection — never the shared pool, whose `max_size=5`
(`connection.py`) is too small to pin a connection for minutes — so that
when its holder process dies, Postgres releases the lock automatically.
That is how a crash becomes `interrupted` with no timeout to tune.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

import asyncpg

from treeweft.adapters.postgresql.connection import get_pool

logger = logging.getLogger(__name__)

LockMode = Literal["exclusive", "shared"]

# The advisory-lock key, computed by Postgres's own hashtext() inline in
# every statement below — never reimplemented in Python — so acquire(),
# release() and probe() are guaranteed to agree on the same key even
# though they may run on different connections. For a two-integer
# advisory lock, `pg_locks.classid`/`objid` are exactly these two
# hashtext() values (Postgres represents key1 as classid, key2 as objid).
_LOCK_KEY_EXPR = "hashtext('treeweft'), hashtext('index-maintenance')"

_warned_no_postgres = False


@dataclass
class MaintenanceLockHandle:
    """A held maintenance lock.

    `coordinated` is False when there was no Postgres to coordinate
    through: `release()` is then a no-op and the caller proceeds
    uncoordinated. That is safe because the job queue itself requires
    Postgres, so at most one process can be doing index work at all in
    that case (research R13).
    """

    mode: LockMode
    coordinated: bool
    _conn: "asyncpg.Connection | None" = None

    async def release(self) -> None:
        if self._conn is None:
            return
        fn = "pg_advisory_unlock_shared" if self.mode == "shared" else "pg_advisory_unlock"
        conn, self._conn = self._conn, None
        try:
            await conn.fetchval(f"SELECT {fn}({_LOCK_KEY_EXPR})")
        finally:
            await conn.close()


async def acquire(mode: LockMode) -> "MaintenanceLockHandle | None":
    """Try to take the maintenance lock in `mode`.

    Returns None only when Postgres is present and another holder
    conflicts (research R13). With no `DATABASE_URL`/pool, returns a
    no-op handle instead of None, and logs a WARNING once per process
    (not once per call).
    """
    global _warned_no_postgres
    pool = await get_pool()
    if pool is None:
        if not _warned_no_postgres:
            logger.warning(
                "maintenance lock: no Postgres pool; proceeding uncoordinated "
                "(safe only with a single indexer process — research R13)"
            )
            _warned_no_postgres = True
        return MaintenanceLockHandle(mode=mode, coordinated=False)

    database_url = os.environ.get("DATABASE_URL", "")
    conn = await asyncpg.connect(database_url)
    fn = "pg_try_advisory_lock_shared" if mode == "shared" else "pg_try_advisory_lock"
    try:
        granted = await conn.fetchval(f"SELECT {fn}({_LOCK_KEY_EXPR})")
    except Exception:
        await conn.close()
        raise
    if not granted:
        await conn.close()
        return None
    return MaintenanceLockHandle(mode=mode, coordinated=True, _conn=conn)


async def probe() -> "LockMode | None":
    """Read the lock's current mode without taking it.

    None when it is unheld, or when there is no Postgres to read
    `pg_locks` through. Only `refresh()` and `dispatch_allowed()` call
    this (research R13) — request paths never do.
    """
    pool = await get_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT mode FROM pg_locks WHERE locktype = 'advisory' "
            f"AND classid = hashtext('treeweft') AND objid = hashtext('index-maintenance') "
            "AND granted LIMIT 1"
        )
    if row is None:
        return None
    return "exclusive" if row["mode"] == "ExclusiveLock" else "shared"
