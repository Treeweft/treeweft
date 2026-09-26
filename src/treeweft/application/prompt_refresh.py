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
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import asyncpg

from treeweft.application import index_guard
from treeweft.application import indexer_runners as runners
from treeweft.application import indexer_state as _state
from treeweft.application import prompt_pins
from treeweft.application import routes_webhook as _routes_webhook
from treeweft.domain.jobs import JobStatus
from treeweft.domain.prompt_pins import is_stale
from treeweft.infrastructure import metrics

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

    # Authoritative read: this process's view can lag another process's
    # write to the pin by up to PROMPT_PINS_REFRESH_SECONDS. Reload before
    # deciding staleness so the hook never skips a refresh a fresher read
    # would have caught, or enqueues one against an already-superseded target.
    await prompt_pins.reload()

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


# ---------------------------------------------------------------------------
# Refresh preemption (code-review finding #6): index work must not be
# silently dropped while a `resummarize` refresh owns a source's only
# "active job" slot (jobs_active_source_uniq, migration 008).
# ---------------------------------------------------------------------------


@dataclass
class PreemptOutcome:
    """Result of `preempt_active_refresh` — the job it created plus how."""

    job: dict
    # "queued": no resummarize was active, or its queued row was cancelled in
    #   time and the incoming job was enqueued exactly as the route would today.
    # "waiting": the resummarize was already running (or won the claim race);
    #   the incoming job was persisted with status=waiting and left out of
    #   job_queue, to be promoted once the refresh reaches a terminal status.
    mode: str
    preempted_job_id: str | None = None


async def preempt_active_refresh(
    source_id: str, build_job: Callable[[], Awaitable[dict]],
) -> PreemptOutcome:
    """Let index work preempt an active `resummarize` refresh for `source_id`.

    `build_job` is an async callable that builds (and, if the route attaches
    a job group, does so) the incoming job dict exactly as the call site
    would today — but does not persist or enqueue it; this function owns
    that step, so it can choose "queued" or "waiting" atomically.

    Call sites must call this ONLY when `_find_active_job_for_source` found
    an active job of kind `resummarize` — for any other kind, callers keep
    today's dedup response unchanged. This function re-checks the active job
    itself (case 3 in the design: it may have finished, or no longer be a
    resummarize, between the caller's check and this call), so it degrades
    gracefully to a plain enqueue when there is nothing left to preempt.
    """
    refresh = await _routes_webhook._find_active_job_for_source(source_id)
    if refresh is None or refresh.get("kind") != "resummarize":
        job = await build_job()
        await runners._persist_job(job)
        await _state._job_queue.enqueue(job["job_id"])
        return PreemptOutcome(job=job, mode="queued", preempted_job_id=None)

    refresh_id = refresh["job_id"]
    job = await build_job()

    canceled = await _state._job_store.cancel_if_queued(
        refresh_id, message=f"preempted by {job.get('kind') or 'index'} job",
    )
    if canceled:
        # The cancelled refresh was counted by `_new_job` and will now never
        # run to its own decrement — drop it here so queue_depth doesn't
        # drift upward on every preemption.
        metrics.queue_depth.dec()
        await runners._persist_job(job)
        await _state._job_queue.enqueue(job["job_id"])
        return PreemptOutcome(job=job, mode="queued", preempted_job_id=refresh_id)

    # Either genuinely running, or a worker claimed it (queued -> running)
    # between our check and the cancel attempt — both fall through here.
    job["status"] = "waiting"
    job["payload"] = {**(job.get("payload") or {}), "after_job": refresh_id}
    job["message"] = "queued after the running summary refresh"
    await runners._persist_job(job)

    # Lost-wakeup guard: the refresh may have reached a terminal status (and
    # its own post-job hook found no waiters yet, since this job didn't
    # exist) between our check above and the persist just above. Re-read it
    # and promote immediately rather than leaving this job stranded until
    # the next unrelated job for the source finishes, or forever.
    refreshed = await _state._job_store.get(refresh_id)
    if refreshed is None or refreshed.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
        await promote_waiting_after(refresh_id)

    return PreemptOutcome(job=job, mode="waiting", preempted_job_id=refresh_id)


async def promote_waiting_after(after_job_id: str) -> list[str]:
    """Promote the oldest job waiting on `after_job_id` once that job is
    terminal, and chain the rest behind it, so every request runs in order.

    Superseding older requests would lose work: two incremental pushes carry
    different changed files. Returns the promoted job_id ([] if none).

    Delegates to `JobStore.promote_next_waiting`, which does the read,
    the active-source check, the conditional promote, the relink, and the
    job_queue insert in ONE transaction (code-review findings C/E): reading
    the waiting rows here and upserting them back as separate round trips
    could relink an already-stranded chain onto a stale snapshot, race a
    concurrent plain enqueue into `jobs_active_source_uniq`, or double
    promote under two concurrent callers.
    """
    store = _state._job_store
    if store is None or not after_job_id:
        return []

    promoted = await store.promote_next_waiting(after_job_id)
    return [promoted] if promoted else []
