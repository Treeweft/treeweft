"""Postgres-backed job queue for cross-process job pickup.

Workers in N indexer processes all hit the same `job_queue` table and pop
rows with `DELETE ... RETURNING` filtered by `FOR UPDATE SKIP LOCKED`, so
two workers never grab the same job. The actual job state (kind,
source_path, etc.) lives in the `jobs` table — this queue carries only
pickup ordering.

Recovery model: a worker DELETEs its claimed row before running the job.
If the worker crashes mid-run, the queue row is gone but the JobStore row
remains in `RUNNING` status, and the next indexer-process startup will
re-enqueue it via the recovery loop in `indexer_service.startup()`.
"""

from __future__ import annotations

import asyncio
import logging
import os

from treeweft.adapters.postgresql.connection import get_pool
from treeweft.domain.jobs import JobQueue

logger = logging.getLogger(__name__)


_POOL_REQUIRED = (
    "PostgresJobQueue requires DATABASE_URL to be set and reachable. "
    "Set DATABASE_URL=postgresql://... in your environment."
)


class PostgresJobQueue(JobQueue):
    def __init__(
        self,
        max_workers: int | None = None,
        poll_interval: float | None = None,
    ) -> None:
        self._max_workers = max_workers or int(os.environ.get("INDEX_CONCURRENCY", "8"))
        self._poll_interval = poll_interval or float(os.environ.get("JOB_QUEUE_POLL_INTERVAL", "1.0"))
        self._workers: list[asyncio.Task] = []
        self._running = False
        self._active_count = 0

    async def start(self) -> None:
        pool = await get_pool()
        if pool is None:
            raise RuntimeError(_POOL_REQUIRED)
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker(i))
            for i in range(self._max_workers)
        ]
        logger.info("PostgresJobQueue started with %d workers", self._max_workers)

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("PostgresJobQueue stopped")

    async def enqueue(self, job_id: str) -> None:
        """Add a job_id to the queue. The Job row must already exist in `jobs`."""
        if not job_id:
            raise ValueError("job_id is required")
        pool = await get_pool()
        if pool is None:
            raise RuntimeError(_POOL_REQUIRED)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO job_queue (job_id) VALUES ($1) ON CONFLICT DO NOTHING",
                job_id,
            )

    def pending_count(self) -> int:
        # Snapshot-only; for accurate cross-process counts, query the table.
        return 0

    def active_count(self) -> int:
        return self._active_count

    async def depth(self) -> int:
        """Return the number of jobs currently waiting in the job_queue table."""
        pool = await get_pool()
        if pool is None:
            return 0
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM job_queue")
            return int(row["cnt"]) if row else 0

    async def _claim_one(self) -> str | None:
        pool = await get_pool()
        if pool is None:
            return None
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                DELETE FROM job_queue
                WHERE job_id = (
                    SELECT job_id FROM job_queue
                    ORDER BY enqueued_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING job_id
                """,
            )
        return row["job_id"] if row else None

    async def _run_one(self, worker_id: int, job_id: str) -> None:
        # Imported lazily to avoid the indexer_service ↔ queue circular import.
        from treeweft.application import indexer_service as idx
        # Job state lives in indexer_state now; reached through the module so
        # the startup rebinding is visible here.
        from treeweft.application import index_guard
        from treeweft.application import indexer_state as idx_state
        from treeweft.application import indexer_runners as idx_runners
        from treeweft.domain.jobs import JobStatus
        import time

        if idx_state._job_store is None:
            logger.error("Worker %d: job store not initialized; abandoning %s", worker_id, job_id)
            return

        job = await idx_state._job_store.get(job_id)
        if job is None:
            logger.warning("Worker %d: claimed job_id=%s not in JobStore; skipping", worker_id, job_id)
            return

        # ADR-004 §3 (research R5 §2): persist `running` BEFORE the
        # authoritative dispatch_allowed() check, so a concurrent rebuild's
        # blocker re-check is guaranteed to see this job if it started first.
        jd = job.to_dict()
        jd["status"] = "running"
        await idx_runners._persist_job(jd)

        if not await index_guard.dispatch_allowed(job):
            jd["status"] = "failed"
            jd["error"] = index_guard.refusal_detail(job.id)
            jd["finished_at"] = time.time()
            await idx_runners._persist_job(jd)
            logger.warning(
                "Worker %d: job %s refused by index_guard (kind=%s)", worker_id, job_id, job.kind
            )
            await self._maybe_enqueue_refresh(worker_id, job)
            return

        coro = idx_runners.dispatch_job(job)
        if coro is None:
            jd = job.to_dict()
            jd["status"] = "failed"
            jd["error"] = f"Cannot dispatch {job.kind!r} job from queue"
            jd["finished_at"] = time.time()
            await idx_runners._persist_job(jd)
            logger.warning("Worker %d: cannot dispatch job %s (kind=%s)", worker_id, job_id, job.kind)
            await self._maybe_enqueue_refresh(worker_id, job)
            return

        self._active_count += 1
        exc_raised: BaseException | None = None
        try:
            await coro
        except Exception as exc:
            exc_raised = exc
            logger.exception("Worker %d: job %s raised", worker_id, job_id)
        finally:
            self._active_count -= 1

        if exc_raised is not None and job.kind in idx._RETRYABLE_KINDS:
            # Increment attempt counter in DB and check against cap.
            new_attempts = await idx_state._job_store.increment_attempts(job_id)
            if new_attempts < idx.MAX_JOB_ATTEMPTS:
                # Re-enqueue for another attempt. Not terminal -- the
                # refresh hook must not fire here (research R4): a still
                # in-flight retry could otherwise race the refresh it starts.
                logger.warning(
                    "Worker %d: retryable job %s (kind=%s) attempt %d/%d — re-enqueuing",
                    worker_id, job_id, job.kind, new_attempts, idx.MAX_JOB_ATTEMPTS,
                )
                jd = job.to_dict()
                jd["status"] = "queued"
                jd["attempts"] = new_attempts
                # Clear the terminal markers a prior _fail_job set, so the
                # re-queued attempt doesn't carry a stale finished_at/error.
                jd["finished_at"] = None
                jd["error"] = ""
                await idx_runners._persist_job(jd)
                await self.enqueue(job_id)
            else:
                # Dead-letter: exhausted all attempts.
                logger.error(
                    "Worker %d: job %s (kind=%s) dead-lettered after %d attempts",
                    worker_id, job_id, job.kind, new_attempts,
                )
                jd = job.to_dict()
                jd["status"] = "dead_letter"
                jd["attempts"] = new_attempts
                jd["finished_at"] = time.time()
                await idx_runners._persist_job(jd)
                from treeweft.infrastructure import metrics
                metrics.dead_letter_jobs_total.inc()
                metrics.incremental_jobs_total.labels(status="dead_letter").inc()
                await self._maybe_enqueue_refresh(worker_id, job)
        elif exc_raised is not None:
            # A non-retryable kind raised out of its runner. Every runner
            # normally catches its own exceptions and calls `_fail_job`
            # internally (so this is a defensive fallback), which means the
            # terminal status was never persisted -- do that here, awaited,
            # before the hook: otherwise `enqueue_if_stale` could still find
            # this job "active" and refuse to enqueue the replacement.
            jd = job.to_dict()
            jd["status"] = "failed"
            jd["error"] = f"{type(exc_raised).__name__}: {exc_raised}"
            jd["finished_at"] = time.time()
            await idx_runners._persist_job(jd)
            await self._maybe_enqueue_refresh(worker_id, job)
        else:
            # A clean finish (done, or done with errors -- the runner
            # itself already persisted the terminal status/error count).
            await self._maybe_enqueue_refresh(worker_id, job)

    async def _maybe_enqueue_refresh(self, worker_id: int, job) -> None:
        """After every terminal status of a job (research R4, amended by
        review finding #2), enqueue a chunk-summary refresh for its source
        if it is now stale.

        For a `resummarize` job itself, this only fires when the deployment
        pin moved past the target this job was chasing: after an
        authoritative `prompt_pins.reload()`, the source's effective version
        is compared against the job's own `payload["target_version"]`
        (re-read from the job store so a payload the runner updated mid-run
        is seen, not this stale in-memory `job`). Equal versions mean the
        pin never moved past what this refresh already targeted, so
        re-enqueuing would just requeue the same failing refresh forever.

        A failure here is logged and never changes the finished job
        (constitution V): the job's own status/error was already persisted
        before this runs, and nothing below touches it.
        """
        source_id = getattr(job, "source_id", None)
        if not source_id:
            return

        if job.kind == "resummarize":
            try:
                from treeweft.application import indexer_state as idx_state
                from treeweft.application import prompt_pins
                from treeweft.application import prompt_refresh

                await prompt_pins.reload()
                store = idx_state._job_store
                fresh = await store.get(job.id) if store is not None else None
                payload = (fresh.payload if fresh is not None else job.payload) or {}
                target = payload.get("target_version")
                current = prompt_pins.effective("chunk_summary", source_id)
                if current == target:
                    return
                await prompt_refresh.enqueue_if_stale(source_id)
            except Exception:
                logger.exception(
                    "Worker %d: resummarize overtaken-check for %s failed after job %s",
                    worker_id, source_id, job.id,
                )
            return

        try:
            from treeweft.application import prompt_refresh
            await prompt_refresh.enqueue_if_stale(source_id)
        except Exception:
            logger.exception(
                "Worker %d: prompt_refresh.enqueue_if_stale(%s) failed after job %s (kind=%s)",
                worker_id, source_id, job.id, job.kind,
            )

    async def _worker(self, worker_id: int) -> None:
        while self._running:
            try:
                job_id = await self._claim_one()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Worker %d: claim failed; backing off", worker_id)
                try:
                    await asyncio.sleep(self._poll_interval)
                except asyncio.CancelledError:
                    break
                continue

            if job_id is None:
                try:
                    await asyncio.sleep(self._poll_interval)
                except asyncio.CancelledError:
                    break
                continue

            try:
                await self._run_one(worker_id, job_id)
            except asyncio.CancelledError:
                break
            except Exception:
                # Without this the exception escapes the `while` loop and kills
                # this worker for the life of the process -- silently, since an
                # un-retrieved task exception is only surfaced at GC. Eight such
                # jobs would disable indexing entirely with nothing in the log.
                logger.exception(
                    "Worker %d: unexpected error handling job %s; "
                    "worker continues", worker_id, job_id,
                )
