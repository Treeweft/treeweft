"""PostgreSQL-backed JobStore — replaces the previous SQLite jobs.db.

Persists Job rows across indexer restarts so RUNNING/QUEUED jobs can be
resumed (or marked failed) on next startup. Interface matches the old
sqlite JobStore so the call sites in indexer_service.py remain unchanged.
"""

from __future__ import annotations

import json
import time

import asyncpg

from treeweft.adapters.postgresql.connection import get_pool
from treeweft.domain.jobs import Job, JobStatus


async def _require_pool():
    """Return the asyncpg pool, or fail loud.

    Job state lives in Postgres and the indexer is documented as failing loud
    when it is unreachable. Ten of the eleven call sites in this module went
    straight from `get_pool()` to `pool.acquire()`, so a None pool surfaced as
    `AttributeError: 'NoneType' object has no attribute 'acquire'` from
    whatever happened to touch it first — a traceback that says nothing about
    the database, raised in a background worker where it reads as an
    unrelated crash while job state is silently lost (CWE-754).
    """
    pool = await get_pool()
    if pool is None:
        raise RuntimeError(
            "PostgreSQL pool unavailable — job state requires DATABASE_URL"
        )
    return pool


class JobStore:
    """asyncpg-backed jobs table.

    `init()` is a no-op apart from verifying that the connection pool is
    live — schema creation lives in `migrations/004_jobs.sql`, applied by
    `adapters.postgresql.run_migrations()` during indexer startup.
    """

    async def init(self) -> None:
        pool = await _require_pool()

    @staticmethod
    def _row_to_job(row) -> Job:
        # payload is stored as JSONB; asyncpg may return it as a string or dict.
        raw_payload = row["payload"] if "payload" in row.keys() else None
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload)
            except Exception:
                raw_payload = None
        attempts_val = row["attempts"] if "attempts" in row.keys() else 0
        group_id_val = row["group_id"] if "group_id" in row.keys() else None
        return Job(
            id=row["id"],
            source=row["source"],
            source_id=row["source_id"],
            status=JobStatus(row["status"]),
            kind=row["kind"] or "repo",
            total_files=row["total_files"],
            processed_files=row["processed_files"],
            committed_files=row["committed_files"] or 0,
            total_chunks=row["total_chunks"],
            start_time=row["start_time"],
            finished_at=row["finished_at"],
            error=row["error"],
            message=row["message"],
            current_file=row["current_file"],
            source_path=row["source_path"],
            source_url=row["source_url"] or "",
            source_branch=row["source_branch"],
            commit_sha=row["commit_sha"] or "",
            errors=row["errors"],
            payload=raw_payload,
            attempts=attempts_val or 0,
            group_id=group_id_val,
        )

    async def upsert(self, job: Job) -> None:
        d = job.to_dict()
        if not d["id"]:
            raise ValueError("Job.id is empty — refusing to persist (id collision risk)")
        # Encode payload dict to JSON string for asyncpg JSONB parameter.
        payload_json = json.dumps(d["payload"]) if d["payload"] is not None else None
        pool = await _require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jobs (
                    id, source, source_id, status, kind,
                    total_files, processed_files, committed_files, total_chunks,
                    start_time, finished_at, error, message, current_file,
                    source_path, source_url, source_branch, commit_sha, errors,
                    payload, attempts, group_id
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9,
                    $10, $11, $12, $13, $14,
                    $15, $16, $17, $18, $19,
                    $20::jsonb, $21, $22
                )
                ON CONFLICT (id) DO UPDATE SET
                    source = EXCLUDED.source,
                    source_id = EXCLUDED.source_id,
                    status = EXCLUDED.status,
                    kind = EXCLUDED.kind,
                    total_files = EXCLUDED.total_files,
                    processed_files = EXCLUDED.processed_files,
                    committed_files = EXCLUDED.committed_files,
                    total_chunks = EXCLUDED.total_chunks,
                    start_time = EXCLUDED.start_time,
                    finished_at = EXCLUDED.finished_at,
                    error = EXCLUDED.error,
                    message = EXCLUDED.message,
                    current_file = EXCLUDED.current_file,
                    source_path = EXCLUDED.source_path,
                    source_url = EXCLUDED.source_url,
                    source_branch = EXCLUDED.source_branch,
                    commit_sha = EXCLUDED.commit_sha,
                    errors = EXCLUDED.errors,
                    payload = EXCLUDED.payload,
                    attempts = EXCLUDED.attempts,
                    -- never clobber an existing group_id with a NULL re-upsert
                    group_id = COALESCE(EXCLUDED.group_id, jobs.group_id)
                """,
                d["id"], d["source"], d["source_id"], d["status"], d["kind"],
                d["total_files"], d["processed_files"], d["committed_files"], d["total_chunks"],
                d["start_time"], d["finished_at"], d["error"], d["message"], d["current_file"],
                d["source_path"], d["source_url"], d["source_branch"], d["commit_sha"], d["errors"],
                payload_json, d["attempts"], d.get("group_id"),
            )

    async def get(self, job_id: str) -> Job | None:
        pool = await _require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
            return self._row_to_job(row) if row else None

    async def list_all(self) -> list[Job]:
        pool = await _require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM jobs")
            return [self._row_to_job(r) for r in rows]

    async def list_by_status(self, status: JobStatus) -> list[Job]:
        pool = await _require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM jobs WHERE status = $1", status.value)
            return [self._row_to_job(r) for r in rows]

    async def list_by_group(self, group_id: str) -> list[Job]:
        """All Tasks (jobs) belonging to a job_group, newest first."""
        pool = await _require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM jobs WHERE group_id = $1 ORDER BY start_time DESC",
                group_id,
            )
            return [self._row_to_job(r) for r in rows]

    async def find_active_for_source(self, source_id: str) -> Job | None:
        """Return any queued or running job for this source, if one exists.

        Used by the indexer endpoints to refuse duplicate submissions while
        a job for the same source is in flight. Cross-worker because it hits
        Postgres rather than any one process's in-memory map.
        """
        if not source_id:
            return None
        pool = await _require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM jobs "
                "WHERE source_id = $1 AND status IN ('queued', 'running') "
                "ORDER BY start_time DESC LIMIT 1",
                source_id,
            )
            return self._row_to_job(row) if row else None

    async def find_active_for_sources(self, source_ids: list[str]) -> dict[str, Job]:
        """Return the active (queued/running) job for each source in
        `source_ids` that has one, keyed by source_id.

        One query instead of N `find_active_for_source` calls — used by
        `routes_prompts.get_prompt_versions`, which otherwise did one round
        trip per listed source.
        """
        if not source_ids:
            return {}
        pool = await _require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT ON (source_id) * FROM jobs "
                "WHERE source_id = ANY($1) AND status IN ('queued', 'running') "
                "ORDER BY source_id, start_time DESC",
                source_ids,
            )
        return {row["source_id"]: self._row_to_job(row) for row in rows}

    async def cancel_if_queued(self, job_id: str, message: str) -> bool:
        """Atomically cancel `job_id` iff a job_queue row for it still
        exists: drop that row and mark the job `done` with `message`.

        Queue-row ownership, not `jobs.status`, is the claim: the worker's
        `_claim_one` DELETEs the job_queue row before it ever upserts
        `running` (`postgres_queue.PostgresJobQueue._run_one`), so a job
        whose row is already gone is already owned by a worker even though
        `jobs.status` may still read `queued` for a few more instructions.
        Returns False (no-op) when no row was deleted — the caller falls
        through to the waiting-job path (ADR-003 preemption case 2)."""
        pool = await _require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "DELETE FROM job_queue WHERE job_id = $1 RETURNING job_id",
                    job_id,
                )
                if row is None:
                    return False
                await conn.execute(
                    "UPDATE jobs SET status = 'done', message = $2, finished_at = $3 "
                    "WHERE id = $1 AND status = 'queued'",
                    job_id, message, time.time(),
                )
                return True

    async def promote_next_waiting(self, after_job_id: str) -> str | None:
        """Promote the oldest job waiting on `after_job_id`, atomically.

        One transaction, holding a row lock on every waiting candidate
        (`FOR UPDATE`) for the duration:

        1. No waiting jobs -> None.
        2. The source already has an active (queued/running) job -- a plain
           enqueue won the race while this job was in flight -- relink every
           waiter to that job's id instead of promoting, and return None.
        3. Otherwise, conditionally flip the oldest waiter to `queued`
           (`WHERE status = 'waiting'`, so a concurrent promoter that got
           there first is a no-op here), relink the rest behind it, insert
           its job_queue row, and return its id.

        A concurrent plain enqueue can land between step 2's check and
        step 3's UPDATE and trip `jobs_active_source_uniq`
        (`asyncpg.UniqueViolationError`) -- caught once and retried from
        step 1, which then takes branch 2 against the now-visible winner.
        """
        if not after_job_id:
            return None
        pool = await _require_pool()
        for attempt in range(2):
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        rows = await conn.fetch(
                            "SELECT * FROM jobs WHERE status = 'waiting' "
                            "AND payload ->> 'after_job' = $1 "
                            "ORDER BY start_time ASC FOR UPDATE",
                            after_job_id,
                        )
                        if not rows:
                            return None

                        waiting_ids = [r["id"] for r in rows]
                        source_id = rows[0]["source_id"]

                        active = await conn.fetchrow(
                            "SELECT id FROM jobs WHERE source_id = $1 "
                            "AND status IN ('queued', 'running') LIMIT 1",
                            source_id,
                        )
                        if active is not None:
                            await conn.execute(
                                "UPDATE jobs SET payload = jsonb_set("
                                "payload, '{after_job}', to_jsonb($2::text)) "
                                "WHERE id = ANY($1::text[])",
                                waiting_ids, active["id"],
                            )
                            return None

                        first_id = waiting_ids[0]
                        rest_ids = waiting_ids[1:]

                        claimed = await conn.fetchrow(
                            "UPDATE jobs SET status = 'queued' "
                            "WHERE id = $1 AND status = 'waiting' RETURNING id",
                            first_id,
                        )
                        if claimed is None:
                            return None

                        if rest_ids:
                            await conn.execute(
                                "UPDATE jobs SET payload = jsonb_set("
                                "payload, '{after_job}', to_jsonb($2::text)) "
                                "WHERE id = ANY($1::text[])",
                                rest_ids, first_id,
                            )

                        await conn.execute(
                            "INSERT INTO job_queue (job_id) VALUES ($1) "
                            "ON CONFLICT DO NOTHING",
                            first_id,
                        )
                        return first_id
            except asyncpg.UniqueViolationError:
                if attempt == 0:
                    continue
                raise
        return None

    async def find_waiting_after(self, after_job_id: str) -> list[Job]:
        """Jobs with status='waiting' whose payload->>'after_job' is
        `after_job_id`, oldest first. Used at a resummarize job's batch
        boundary (has any follower arrived?) and by promotion (which one(s)
        to enqueue) once that job reaches a terminal status."""
        if not after_job_id:
            return []
        pool = await _require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM jobs WHERE status = 'waiting' "
                "AND payload ->> 'after_job' = $1 ORDER BY start_time ASC",
                after_job_id,
            )
            return [self._row_to_job(r) for r in rows]

    async def update_status(self, job_id: str, status: JobStatus) -> None:
        pool = await _require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET status = $1 WHERE id = $2",
                status.value, job_id,
            )

    async def increment_attempts(self, job_id: str) -> int:
        """Atomically increment the attempts counter and return the new value."""
        pool = await _require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE jobs SET attempts = attempts + 1 WHERE id = $1 RETURNING attempts",
                job_id,
            )
            return row["attempts"] if row else 0

    async def list_dead_letter(self) -> list[Job]:
        """Return all jobs in DEAD_LETTER status, newest first."""
        pool = await _require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM jobs WHERE status = 'dead_letter' ORDER BY finished_at DESC NULLS LAST",
            )
            return [self._row_to_job(r) for r in rows]

    async def update_progress(
        self, job_id: str, processed_files: int, total_chunks: int,
    ) -> None:
        pool = await _require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET processed_files = $1, total_chunks = $2 WHERE id = $3",
                processed_files, total_chunks, job_id,
            )

    async def close(self) -> None:
        # Pool lifecycle is owned by treeweft.adapters.postgresql.connection.
        # Nothing per-store to close.
        return None
