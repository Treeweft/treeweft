"""Prompt-pin resolution and Postgres sync for the running process (ADR-003).

Holds one module-level `PinView`, swapped atomically on load and on every
reload. `effective()` is a pure dict lookup — it never touches Postgres.
Without `DATABASE_URL`, the view stays at the baseline versions forever
(research R9): an upgrade with no Postgres changes no prompt.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from treeweft.adapters.llm_api import prompts
from treeweft.adapters.postgresql.connection import get_pool
from treeweft.adapters.postgresql.prompt_pin_store import NOTIFY_CHANNEL, PromptPinStore
from treeweft.adapters.sources.repository import PostgreSourceRepository
from treeweft.application import indexer_state as _state
from treeweft.domain.prompt_pins import PinView, resolve, seed_version
from treeweft.infrastructure.config import prompt_pins_refresh_seconds

logger = logging.getLogger(__name__)

_NO_DATABASE_WARNING = (
    "prompt pins unavailable without DATABASE_URL; using baseline chunk_summary v3, hyde v1"
)

_SCHEMA_CHECK_SQL = """
    SELECT
        to_regclass('prompt_pins') IS NOT NULL AS has_table,
        EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'source_records' AND column_name = 'summary_prompt_version'
        ) AS has_version_column,
        EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'source_records' AND column_name = 'summary_refresh_target'
        ) AS has_target_column
"""

_LISTENER_POLL_SECONDS = 5.0
_LISTENER_BACKOFF_INITIAL = 1.0
_LISTENER_BACKOFF_MAX = 30.0

_pin_store = PromptPinStore()
_source_repo = PostgreSourceRepository()

_view = PinView(deployment=dict(prompts.BASELINE), overrides={})

_listener_task: asyncio.Task | None = None
_periodic_task: asyncio.Task | None = None
_listener_conn = None
_stopping = False


def view() -> PinView:
    return _view


def effective(operation: str, source_id: str | None = None) -> int:
    return resolve(_view, operation, source_id)


def _seed_reason(operation: str, histogram: dict[int, int], has_sources: bool) -> str:
    if not has_sources:
        return "no sources; seeded to latest"
    if operation == "hyde":
        return "hyde always seeds to 1"
    if not histogram:
        return "no recorded versions; seeded to latest"
    return "most common recorded version"


def _build_view(rows) -> PinView:
    deployment: dict[str, int] = {}
    overrides: dict[str, int] = {}
    for row in rows:
        if not prompts.is_registered(row.operation, row.version):
            valid = prompts.versions(row.operation) if row.operation in prompts.REGISTRY else ()
            raise RuntimeError(
                f"prompt pin {row.operation}/{row.scope}=v{row.version} is not a registered "
                f"version; registered versions for {row.operation}: {valid}"
            )
        if row.scope == "deployment":
            deployment[row.operation] = row.version
        else:
            overrides[row.scope] = row.version
    return PinView(deployment=deployment, overrides=overrides)


async def _verify_schema() -> None:
    pool = await get_pool()
    if pool is None:
        raise RuntimeError(
            "prompt_pins startup check requires DATABASE_URL to point at a reachable "
            "Postgres instance"
        )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SCHEMA_CHECK_SQL)
    if not (row and row["has_table"] and row["has_version_column"] and row["has_target_column"]):
        raise RuntimeError(
            "prompt_pins table or source_records summary columns are missing; run migration "
            "021 (021_prompt_versions.sql) before starting"
        )


async def load_and_seed() -> None:
    """Startup step (research R9).

    Fails loud: raises on a missing schema or an unregistered stored pin.
    Writes nothing and logs one warning without `DATABASE_URL`.
    """
    global _view

    if not _state.DATABASE_URL:
        logger.warning(_NO_DATABASE_WARNING)
        return

    await _verify_schema()

    rows = await _pin_store.list_all()
    seeded_ops = {row.operation for row in rows if row.scope == "deployment"}
    missing = [op for op in prompts.REGISTRY if op not in seeded_ops]

    if missing:
        has_sources = bool(await _source_repo.list_all())
        histogram = await _source_repo.summary_version_histogram()
        for op in missing:
            version = seed_version(op, histogram, has_sources, prompts.latest(op).version)
            await _pin_store.seed_if_absent(op, version)
            logger.info(
                "seeded prompt pin %s=%d (%s)", op, version, _seed_reason(op, histogram, has_sources)
            )
        rows = await _pin_store.list_all()

    _view = _build_view(rows)


async def reload() -> None:
    """Re-read every pin and swap the view.

    Never raises: an unregistered stored pin (or any other read failure) is
    logged at ERROR and the previous view is kept — startup is where
    validation fails loud.
    """
    if not _state.DATABASE_URL:
        return

    global _view
    try:
        rows = await _pin_store.list_all()
        new_view = _build_view(rows)
    except Exception as exc:
        logger.error("prompt pin reload failed, keeping previous view: %s", exc)
        return

    old_hyde = _view.deployment.get("hyde")
    _view = new_view
    if new_view.deployment.get("hyde") != old_hyde:
        from treeweft.adapters.llm_api.llm_adapter import _HYDE_CACHE

        _HYDE_CACHE.clear()


async def _listener_supervisor(dsn: str) -> None:
    global _listener_conn

    backoff = _LISTENER_BACKOFF_INITIAL
    while not _stopping:
        try:
            _listener_conn = await asyncpg.connect(dsn)

            def _on_notify(_conn, _pid, _channel, _payload):
                asyncio.create_task(reload())

            await _listener_conn.add_listener(NOTIFY_CHANNEL, _on_notify)
            logger.info("[prompt_pins] listening on Postgres channel %r", NOTIFY_CHANNEL)
            backoff = _LISTENER_BACKOFF_INITIAL
            await reload()  # pick up anything missed while disconnected

            while not _stopping:
                if _listener_conn.is_closed():
                    raise ConnectionError("listener connection closed")
                await asyncio.sleep(_LISTENER_POLL_SECONDS)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            if _stopping:
                break
            logger.warning(
                "[prompt_pins] listener disconnected (%s); reconnecting in %.1fs", exc, backoff
            )
            try:
                if _listener_conn is not None and not _listener_conn.is_closed():
                    await _listener_conn.close()
            except Exception:
                pass
            _listener_conn = None
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2, _LISTENER_BACKOFF_MAX)


async def _periodic_reload_loop() -> None:
    while not _stopping:
        try:
            await asyncio.sleep(prompt_pins_refresh_seconds())
        except asyncio.CancelledError:
            break
        if _stopping:
            break
        await reload()


async def start_sync() -> None:
    """Start the LISTEN supervisor and the periodic reload loop.

    No-op without `DATABASE_URL`. Idempotent: replaces any running sync.
    """
    if not _state.DATABASE_URL:
        return

    await stop_sync()

    global _stopping, _listener_task, _periodic_task
    _stopping = False
    _listener_task = asyncio.create_task(_listener_supervisor(_state.DATABASE_URL))
    _periodic_task = asyncio.create_task(_periodic_reload_loop())


async def stop_sync() -> None:
    global _stopping, _listener_task, _periodic_task, _listener_conn

    _stopping = True
    for task in (_listener_task, _periodic_task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    _listener_task = None
    _periodic_task = None

    if _listener_conn is not None:
        try:
            await _listener_conn.close()
        except Exception:
            pass
        _listener_conn = None
