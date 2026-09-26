"""Planning and enqueuing chunk-summary refreshes (ADR-003, research R4).

Two entry points:

- `plan_refreshes` / `enqueue_refreshes`: used by `prompt_pins.set_pin` to
  sort every affected source into `enqueued` / `deferred` / `not_enqueued`
  and then, for a real (non dry-run) call, turn the `enqueued` bucket into
  actual `resummarize` jobs.
- `enqueue_if_stale`: the single-source hook a finished job calls after
  every terminal status (research R4), and the manual refresh endpoint.

Jobs are not serialized (research R4): a source with an active job is never
enqueued again here; it is reported as `deferred`, and the hook re-checks it
once that job finishes. `jobs_active_source_uniq` (migration 008) is the
authoritative guard against a race between two processes reaching the same
conclusion at once — a unique-violation on insert is caught here and turned
into a deferred report, never a 500.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

import asyncpg

from treeweft.application import index_guard
from treeweft.application import indexer_runners as runners
from treeweft.application import indexer_state as _state
from treeweft.application import prompt_pins
from treeweft.application import routes_webhook as _routes_webhook
from treeweft.domain.prompt_pins import is_stale

logger = logging.getLogger(__name__)

_UNIQUE_VIOLATION_ERRORS: tuple[type[BaseException], ...] = (asyncpg.UniqueViolationError,)


@dataclass
class RefreshItem:
    source_id: str
    current_version: int | None
    target_version: int
    chunk_count: int
    job_id: str | None = None
    blocking_job_id: str | None = None
    reason: str | None = None

    def enqueued_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "job_id": self.job_id,
            "current_version": self.current_version,
            "target_version": self.target_version,
            "chunk_count": self.chunk_count,
        }

    def deferred_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "blocking_job_id": self.blocking_job_id,
            "current_version": self.current_version,
            "target_version": self.target_version,
            "chunk_count": self.chunk_count,
        }

    def not_enqueued_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "reason": self.reason,
            "current_version": self.current_version,
            "target_version": self.target_version,
            "chunk_count": self.chunk_count,
        }


@dataclass
class RefreshPlan:
    enqueued: list[RefreshItem] = field(default_factory=list)
    deferred: list[RefreshItem] = field(default_factory=list)
    not_enqueued: list[RefreshItem] = field(default_factory=list)
    group_id: str | None = None

    @property
    def total_chunks(self) -> int:
        return sum(
            i.chunk_count for i in (*self.enqueued, *self.deferred, *self.not_enqueued)
        )


def _guard_reason(gate) -> str:
    try:
        body = json.loads(bytes(gate.body))
    except Exception:
        return "index is not writable"
    status = body.get("index_status", "not writable")
    detail = body.get("reason") or body.get("detail") or "not writable"
    return f"index is {status}: {detail}"


async def plan_refreshes(sources, target_for: Callable[[str], int]) -> RefreshPlan:
    """Sort every stale source in `sources` into enqueue / defer / refuse.

    `target_for(source_id)` returns the version that source should end up
    at; a source is included only when `domain.prompt_pins.is_stale` says it
    is stale toward that target (FR-015: a source mid-refresh is included
    even if its recorded version already equals the target). No job is
    created here — that is `enqueue_refreshes`'s job, so a dry run can call
    this alone and get an identical plan to a real call (SC-004).
    """
    plan = RefreshPlan()

    stale: list[tuple[object, int]] = []
    for source in sources:
        target = target_for(source.id)
        if target is None:
            continue
        if not is_stale(source.summary_prompt_version, source.summary_refresh_target, target):
            continue
        stale.append((source, target))

    if not stale:
        return plan

    gate = await index_guard.require_writable()
    guard_reason = _guard_reason(gate) if gate is not None else None

    for source, target in stale:
        item = RefreshItem(
            source_id=source.id,
            current_version=source.summary_prompt_version,
            target_version=target,
            chunk_count=int(source.chunk_count or 0),
        )
        if guard_reason is not None:
            item.reason = guard_reason
            plan.not_enqueued.append(item)
            continue

        active = await _routes_webhook._find_active_job_for_source(source.id)
        if active:
            item.blocking_job_id = active["job_id"]
            plan.deferred.append(item)
            continue

        plan.enqueued.append(item)

    return plan


async def _attach_single_group(job: dict, created_by: str | None) -> None:
    """Group-of-one, matching `indexer_service._attach_group`'s default
    branch (data-model.md: "a single refresh is a group of one, as for
    other jobs"). Defined locally — `indexer_runners` may not import
    `indexer_service`, and this module sits alongside it."""
    if _state._job_group_store is None:
        return
    job["group_id"] = await _state._job_group_store.create(
        label=job.get("source") or job.get("source_id") or job["id"],
        kind=job.get("kind") or "resummarize",
        created_by=created_by,
        task_count=1,
    )


async def _create_resummarize_job(
    item: RefreshItem, *, created_by: str | None, group_id: str | None
) -> str | None:
    """Persist and enqueue one `resummarize` job for `item`.

    Returns the job_id, or None if a concurrent enqueue for the same
    source already won the `jobs_active_source_uniq` race.
    """
    job = runners._new_job("resummarize", item.source_id, item.source_id)
    job["payload"] = {"target_version": item.target_version}
    job["created_by"] = created_by
    if group_id:
        job["group_id"] = group_id
    else:
        await _attach_single_group(job, created_by)

    try:
        await runners._persist_job(job)
    except _UNIQUE_VIOLATION_ERRORS as exc:
        logger.warning(
            "resummarize enqueue for source %s lost the jobs_active_source_uniq race: %s",
            item.source_id, exc,
        )
        return None

    await _state._job_queue.enqueue(job["job_id"])
    return job["job_id"]


async def enqueue_refreshes(plan: RefreshPlan, *, created_by: str | None) -> RefreshPlan:
    """Turn `plan.enqueued` into real `resummarize` jobs.

    Two or more jobs share one `prompt-refresh` job group (data-model.md);
    a single job gets its own group of one, kind `resummarize`. A source
    that loses the `jobs_active_source_uniq` race is moved to `deferred`
    with the winning job's id, never surfaced as a 500.
    """
    candidates = plan.enqueued
    if not candidates:
        return plan

    group_id: str | None = None
    if len(candidates) >= 2 and _state._job_group_store is not None:
        group_id = await _state._job_group_store.create(
            label="prompt refresh",
            kind="prompt-refresh",
            created_by=created_by,
            task_count=len(candidates),
        )
        plan.group_id = group_id

    still_enqueued: list[RefreshItem] = []
    for item in candidates:
        job_id = await _create_resummarize_job(item, created_by=created_by, group_id=group_id)
        if job_id is None:
            active = await _routes_webhook._find_active_job_for_source(item.source_id)
            item.blocking_job_id = active["job_id"] if active else None
            plan.deferred.append(item)
            continue
        item.job_id = job_id
        still_enqueued.append(item)

    plan.enqueued = still_enqueued
    return plan


async def enqueue_if_stale(source_id: str) -> str | None:
    """Enqueue a `resummarize` job for `source_id` if it is stale, has no
    active job, and the index is writable. Returns the new job_id, or None
    when nothing was enqueued (current, an active job already covers it,
    the index refuses writes, or a concurrent enqueue won the race).

    Called after every terminal status of a non-`resummarize` job
    (research R4), and by the manual refresh endpoint for a single source.
    """
    source = await _state._source_repo.get_by_id(source_id)
    if source is None:
        return None

    target = prompt_pins.effective("chunk_summary", source_id)
    if not is_stale(source.summary_prompt_version, source.summary_refresh_target, target):
        return None

    active = await _routes_webhook._find_active_job_for_source(source_id)
    if active:
        return None

    gate = await index_guard.require_writable()
    if gate is not None:
        return None

    item = RefreshItem(
        source_id=source_id,
        current_version=source.summary_prompt_version,
        target_version=target,
        chunk_count=int(source.chunk_count or 0),
    )
    return await _create_resummarize_job(item, created_by=None, group_id=None)
