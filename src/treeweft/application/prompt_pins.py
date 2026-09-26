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


class UnknownPromptVersionError(ValueError):
    """Raised by `set_pin` for a version that is not registered for the
    operation. Carries `valid_versions` so the API can return the
    contract's 400 `{"detail", "valid_versions"}` body."""

    def __init__(self, operation: str, version: int, valid_versions: tuple[int, ...]):
        super().__init__(f"unknown {operation} version {version}")
        self.operation = operation
        self.version = version
        self.valid_versions = valid_versions


HYDE_OVERRIDE_DETAIL = (
    "hyde pins are deployment-wide; per-source overrides are not supported"
)


class HydeOverrideNotSupportedError(ValueError):
    """Raised by `set_pin` for a hyde pin with a non-deployment scope (US3
    scenario 4). Same refusal for a dry run and a real call — the table's
    `CHECK` enforces the same rule."""

    def __init__(self):
        super().__init__(HYDE_OVERRIDE_DETAIL)


class UnknownSourceError(ValueError):
    """Raised by `set_pin`/`clear_override` for a source_id that does not
    exist. The API translates this to a 404."""

    def __init__(self, source_id: str):
        super().__init__(f"unknown source: {source_id}")
        self.source_id = source_id


class NoOverrideError(ValueError):
    """Raised by `clear_override` when `source_id` exists but carries no
    chunk_summary override. The API translates this to a 404."""

    def __init__(self, source_id: str):
        super().__init__(f"source {source_id} has no chunk_summary override")
        self.source_id = source_id

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

# Serializes reload()'s fetch-build-swap so an older read started before a
# newer one can never complete after it and clobber the fresher view.
_reload_lock = asyncio.Lock()

# Strong references to the NOTIFY callback's fire-and-forget reload() tasks.
# asyncio only holds a weak reference to a task created via create_task; with
# nothing else referencing it, the task can be garbage-collected mid-flight
# (see the asyncio.create_task docs). Each task removes itself on completion.
_notify_tasks: set[asyncio.Task] = set()


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

    Never raises: an unregistered stored pin, a stored pin set that is
    missing a deployment row for a registered operation (e.g. a hand-deleted
    row), or any other read failure is logged at ERROR and the previous view
    is kept — startup is where validation fails loud.

    Concurrent reloads (the NOTIFY listener, the periodic loop, and a caller
    swapping its own view right after a write can all race) are serialized
    by `_reload_lock` so an older read that happens to finish last can never
    overwrite a newer view with stale data.
    """
    if not _state.DATABASE_URL:
        return

    global _view
    async with _reload_lock:
        try:
            rows = await _pin_store.list_all()
            new_view = _build_view(rows)
        except Exception as exc:
            logger.error("prompt pin reload failed, keeping previous view: %s", exc)
            return

        missing = [op for op in prompts.REGISTRY if op not in new_view.deployment]
        if missing:
            logger.error(
                "prompt pin reload missing deployment pin(s) for %s, keeping previous view",
                missing,
            )
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
                task = asyncio.create_task(reload())
                _notify_tasks.add(task)
                task.add_done_callback(_notify_tasks.discard)

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


def _empty_result(operation: str, scope: str, previous_version, version: int, dry_run: bool) -> dict:
    return {
        "dry_run": dry_run,
        "operation": operation,
        "scope": scope,
        "previous_version": previous_version,
        "version": version,
        "enqueued": [],
        "deferred": [],
        "not_enqueued": [],
        "group_id": None,
        "total_chunks": 0,
        "effect": "takes effect on the next query" if operation == "hyde" else None,
    }


def _log_pin_change(
    dry_run: bool,
    caller: str | None,
    operation: str,
    scope: str,
    previous_version,
    version: int,
    result: dict,
) -> None:
    log = logger.info if dry_run else logger.warning
    log(
        "event=prompt_pin_change dry_run=%s caller=%s operation=%s scope=%s "
        "previous_version=%s version=%s enqueued=%s deferred=%s not_enqueued=%s",
        dry_run, caller, operation, scope, previous_version, version,
        [i["source_id"] for i in result["enqueued"]],
        [i["source_id"] for i in result["deferred"]],
        [i["source_id"] for i in result["not_enqueued"]],
    )


async def set_pin(
    operation: str, scope: str, version: int, *, updated_by: str | None, dry_run: bool
) -> dict:
    """Set the deployment pin (`scope == "deployment"`, US2) or a per-source
    chunk_summary override (`scope == <source_id>`, US3).

    HyDE has no per-source overrides: any non-deployment scope raises
    `HydeOverrideNotSupportedError`, for both a dry run and a real call,
    before anything else is validated.

    Validates the version against the registry (raising
    `UnknownPromptVersionError` on a miss, for both a dry run and a real
    call). For an override scope, the source must exist
    (`UnknownSourceError` otherwise). Setting the pin to its already-current
    value at that scope is a no-op: no row is written, `NOTIFY`d, or logged
    as a change.

    A real (non dry-run) call writes the pin, swaps this process's own view
    immediately (`reload()`, which also clears `llm_adapter._HYDE_CACHE` on
    a HyDE change), and enqueues a refresh for every affected source — every
    non-overridden source for a deployment change, or just the one source
    for an override. A dry run computes the identical plan and writes
    nothing (SC-004).
    """
    if operation == "hyde" and scope != "deployment":
        raise HydeOverrideNotSupportedError()

    if not prompts.is_registered(operation, version):
        raise UnknownPromptVersionError(operation, version, prompts.versions(operation))

    # Authoritative read: this process's view can lag another process's
    # write by up to PROMPT_PINS_REFRESH_SECONDS. Reload before the
    # no-op check and the plan so both are decided from Postgres's current
    # pins, not a stale local view.
    await reload()

    if scope == "deployment":
        previous_version = _view.deployment.get(operation)
        sources = [
            s for s in await _source_repo.list_all() if s.id not in _view.overrides
        ]
    else:
        source = await _source_repo.get_by_id(scope)
        if source is None:
            raise UnknownSourceError(scope)
        previous_version = _view.overrides.get(scope)
        sources = [source]

    if previous_version == version:
        return _empty_result(operation, scope, previous_version, version, dry_run)

    if operation == "hyde":
        if not dry_run:
            await _pin_store.upsert(operation, scope, version, updated_by)
            await reload()
        result = _empty_result(operation, scope, previous_version, version, dry_run)
        _log_pin_change(dry_run, updated_by, operation, scope, previous_version, version, result)
        return result

    from treeweft.application import prompt_refresh

    plan = await prompt_refresh.plan_refreshes(sources, lambda _sid: version)

    if not dry_run:
        await _pin_store.upsert(operation, scope, version, updated_by)
        await reload()
        plan = await prompt_refresh.enqueue_refreshes(plan, created_by=updated_by)

    result = {
        "dry_run": dry_run,
        "operation": operation,
        "scope": scope,
        "previous_version": previous_version,
        "version": version,
        "enqueued": [i.enqueued_dict() for i in plan.enqueued],
        "deferred": [i.deferred_dict() for i in plan.deferred],
        "not_enqueued": [i.not_enqueued_dict() for i in plan.not_enqueued],
        "group_id": plan.group_id if not dry_run else None,
        "total_chunks": plan.total_chunks,
        "effect": None,
    }
    _log_pin_change(dry_run, updated_by, operation, scope, previous_version, version, result)
    return result


async def clear_override(source_id: str, *, updated_by: str | None, dry_run: bool) -> dict:
    """Clear a source's chunk_summary override (US3 scenario 3): it then
    follows the deployment pin again.

    Raises `UnknownSourceError` when `source_id` doesn't exist, and
    `NoOverrideError` when it exists but carries no override — both are a
    404 at the API. A real call deletes the pin row (`NOTIFY`), swaps this
    process's view, and enqueues a refresh only if the source is now stale
    toward the deployment pin. A dry run computes the identical plan and
    writes nothing.
    """
    source = await _source_repo.get_by_id(source_id)
    if source is None:
        raise UnknownSourceError(source_id)

    # Authoritative read: see set_pin's comment above.
    await reload()

    previous_version = _view.overrides.get(source_id)
    if previous_version is None:
        raise NoOverrideError(source_id)

    version = _view.deployment["chunk_summary"]

    from treeweft.application import prompt_refresh

    plan = await prompt_refresh.plan_refreshes([source], lambda _sid: version)

    if not dry_run:
        await _pin_store.delete("chunk_summary", source_id)
        await reload()
        plan = await prompt_refresh.enqueue_refreshes(plan, created_by=updated_by)

    result = {
        "dry_run": dry_run,
        "operation": "chunk_summary",
        "scope": source_id,
        "previous_version": previous_version,
        "version": version,
        "enqueued": [i.enqueued_dict() for i in plan.enqueued],
        "deferred": [i.deferred_dict() for i in plan.deferred],
        "not_enqueued": [i.not_enqueued_dict() for i in plan.not_enqueued],
        "group_id": plan.group_id if not dry_run else None,
        "total_chunks": plan.total_chunks,
        "effect": None,
    }
    _log_pin_change(dry_run, updated_by, "chunk_summary", source_id, previous_version, version, result)
    return result
