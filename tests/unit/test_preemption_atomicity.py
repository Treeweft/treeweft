"""Concurrency defects in ADR-003 refresh preemption (code review follow-up).

Covers, each against the production code path (not just the fakes):

- B: `cancel_if_queued` must treat job_queue row ownership, not `jobs.status`,
  as the claim -- a worker that already deleted its queue row owns the job
  even though jobs.status still reads 'queued'. `_run_one` must also skip a
  claimed job_id whose fetched row is already terminal.
- A: `preempt_active_refresh`'s "waiting" branch must not strand the new job
  forever if the refresh it is waiting on finishes between the check and the
  persist -- it must re-read the refresh and promote if terminal.
- C/E: `promote_waiting_after` (delegating to `JobStore.promote_next_waiting`)
  must relink waiters to an already-active job for the source instead of
  double-enqueuing, and two concurrent promotions must promote exactly once.
- D: startup recovery must not promote the followers of a still-`waiting`
  head -- only the head's own eventual promotion may do that.
- F: cancelling a queued refresh must decrement `metrics.queue_depth` once.

Uses the same `InMemoryJobStore`/`InMemoryQueue` fakes as
`test_refresh_preemption.py` (imported from there rather than duplicated).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from treeweft.adapters.queue.postgres_queue import PostgresJobQueue
from treeweft.application import indexer_state as idx_state
from treeweft.application import index_guard as ig
from treeweft.application import lifecycle
from treeweft.application import prompt_refresh
from treeweft.domain.jobs import Job, JobStatus

from tests.unit.test_refresh_preemption import (
    InMemoryJobStore,
    InMemoryQueue,
    _refresh_job,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _writable_index(monkeypatch):
    monkeypatch.setattr(ig, "_status", ig.IndexStatus("ok"))


@pytest.fixture(autouse=True)
def _no_job_group_store(monkeypatch):
    monkeypatch.setattr(idx_state, "_job_group_store", None)


async def _noop():
    return None


# ---------------------------------------------------------------------------
# B: worker claim wins the race; _run_one skips an already-terminal job
# ---------------------------------------------------------------------------


class TestCancelVsClaimRace:
    async def test_worker_claim_wins_route_falls_through_to_waiting(self, monkeypatch):
        """The route's outer check saw 'queued', but the worker already
        deleted the job_queue row (its own upsert to 'running' hasn't landed
        yet) -- cancel_if_queued must return False, not True."""
        store = InMemoryJobStore([_refresh_job(status=JobStatus.QUEUED)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        # Worker's _claim_one already ran: queue row gone, status untouched.
        store.simulate_worker_claim("refresh-1")

        canceled = await store.cancel_if_queued("refresh-1", message="preempted")
        assert canceled is False

        # And the full preemption flow falls through to "waiting", not a
        # second "queued" job racing the unique index.
        async def build():
            return {"job_id": "incoming-1", "id": "incoming-1", "kind": "incremental",
                    "status": "queued", "message": "m"}

        outcome = await prompt_refresh.preempt_active_refresh("src-1", build)
        assert outcome.mode == "waiting"
        assert queue.enqueued == []

    async def test_run_one_skips_already_terminal_job(self, monkeypatch):
        """A job_id claimed off job_queue whose jobs row is already terminal
        (done/failed/dead_letter) must be skipped untouched, not re-run or
        re-persisted."""
        job = Job(id="job-done", kind="incremental", source_id="src-2", status=JobStatus.DONE)
        store = InMemoryJobStore([job])
        monkeypatch.setattr(idx_state, "_job_store", store)

        persisted = []

        async def fake_persist(jd):
            persisted.append(jd)

        from treeweft.application import indexer_runners as runners
        monkeypatch.setattr(runners, "_persist_job", fake_persist)

        dispatched = []
        monkeypatch.setattr(runners, "dispatch_job", lambda j: dispatched.append(j))

        q = PostgresJobQueue(max_workers=1)
        await q._run_one(worker_id=0, job_id="job-done")

        assert persisted == []
        assert dispatched == []
        # Untouched: still DONE.
        assert (await store.get("job-done")).status == JobStatus.DONE

    @pytest.mark.parametrize("status", [JobStatus.FAILED, JobStatus.DEAD_LETTER])
    async def test_run_one_skips_other_terminal_statuses(self, monkeypatch, status):
        job = Job(id="job-x", kind="incremental", source_id="src-3", status=status)
        store = InMemoryJobStore([job])
        monkeypatch.setattr(idx_state, "_job_store", store)

        from treeweft.application import indexer_runners as runners
        called = []
        monkeypatch.setattr(runners, "_persist_job", lambda jd: called.append(jd))

        q = PostgresJobQueue(max_workers=1)
        await q._run_one(worker_id=0, job_id="job-x")

        assert called == []


# ---------------------------------------------------------------------------
# A: lost wakeup -- refresh finishes between the check and the persist
# ---------------------------------------------------------------------------


class TestLostWakeup:
    async def test_waiting_job_promoted_when_refresh_finishes_mid_call(self, monkeypatch):
        store = InMemoryJobStore([_refresh_job(status=JobStatus.RUNNING)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        async def build():
            # Simulate the refresh finishing between preempt_active_refresh's
            # own active-job check (already done by the caller before this
            # is invoked) and the persist of the new waiting job below.
            done = (await store.get("refresh-1")).to_dict()
            done["status"] = "done"
            done["finished_at"] = 1.0
            store.jobs["refresh-1"] = Job.from_dict(done)
            return {"job_id": "incoming-5", "id": "incoming-5", "kind": "incremental",
                    "status": "queued", "message": "m"}

        outcome = await prompt_refresh.preempt_active_refresh("src-1", build)

        assert outcome.mode == "waiting"
        # Without the fix this job is stranded (still WAITING, never
        # enqueued) because the refresh had already gone terminal by the
        # time the waiting job was persisted.
        promoted = await store.get("incoming-5")
        assert promoted.status == JobStatus.QUEUED
        assert queue.enqueued == ["incoming-5"]


# ---------------------------------------------------------------------------
# C/E: non-atomic promotion -- relink to an active job, exactly-once promote
# ---------------------------------------------------------------------------


class TestPromotionAtomicity:
    async def test_relinks_to_already_active_job_for_source(self, monkeypatch):
        """A plain enqueue for the source won the race and is now active;
        promotion must relink the waiters to it, not double-enqueue and not
        raise."""
        active = Job(id="active-1", kind="incremental", source_id="src-c",
                     status=JobStatus.QUEUED, start_time=5.0)
        waiting = Job(id="wait-c", kind="incremental", source_id="src-c",
                      status=JobStatus.WAITING, start_time=1.0,
                      payload={"after_job": "refresh-c"})
        store = InMemoryJobStore([active, waiting])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        result = await prompt_refresh.promote_waiting_after("refresh-c")

        assert result == []
        assert queue.enqueued == []
        relinked = await store.get("wait-c")
        assert relinked.status == JobStatus.WAITING
        assert relinked.payload["after_job"] == "active-1"

    async def test_relinks_all_waiters_not_just_the_oldest(self, monkeypatch):
        older = Job(id="wait-c1", kind="incremental", source_id="src-c2",
                    status=JobStatus.WAITING, start_time=1.0,
                    payload={"after_job": "refresh-c2"})
        newer = Job(id="wait-c2", kind="incremental", source_id="src-c2",
                    status=JobStatus.WAITING, start_time=2.0,
                    payload={"after_job": "refresh-c2"})
        active = Job(id="active-2", kind="incremental", source_id="src-c2",
                     status=JobStatus.RUNNING, start_time=5.0)
        store = InMemoryJobStore([older, newer, active])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        result = await prompt_refresh.promote_waiting_after("refresh-c2")

        assert result == []
        assert queue.enqueued == []
        for jid in ("wait-c1", "wait-c2"):
            j = await store.get(jid)
            assert j.status == JobStatus.WAITING
            assert j.payload["after_job"] == "active-2"

    async def test_concurrent_promotions_promote_exactly_once(self, monkeypatch):
        waiting = Job(id="wait-e", kind="incremental", source_id="src-e",
                      status=JobStatus.WAITING, start_time=1.0,
                      payload={"after_job": "refresh-e"})
        store = InMemoryJobStore([waiting])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        results = await asyncio.gather(
            prompt_refresh.promote_waiting_after("refresh-e"),
            prompt_refresh.promote_waiting_after("refresh-e"),
        )

        # Exactly one of the two concurrent callers actually promoted it.
        non_empty = [r for r in results if r]
        assert non_empty == [["wait-e"]]
        assert queue.enqueued == ["wait-e"]
        assert (await store.get("wait-e")).status == JobStatus.QUEUED


# ---------------------------------------------------------------------------
# D: recovery must not promote followers of a still-waiting head
# ---------------------------------------------------------------------------


class _FakeUserStore:
    async def count_users(self):
        return 1


class _FakeLifecycleJobGroupStore:
    async def init(self):
        pass


class _FakeLifecycleQueue:
    def __init__(self):
        self.enqueued: list[str] = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def enqueue(self, job_id):
        self.enqueued.append(job_id)


class TestRecoverySkipsWaitingHead:
    @pytest.fixture(autouse=True)
    def _restore_job_globals(self):
        job_store = idx_state._job_store
        job_group_store = idx_state._job_group_store
        job_queue = idx_state._job_queue
        yield
        idx_state._job_store = job_store
        idx_state._job_group_store = job_group_store
        idx_state._job_queue = job_queue

    async def _run_startup(self, monkeypatch, *, job_store: InMemoryJobStore):
        from treeweft.application import indexer_runners as runners

        app = SimpleNamespace(state=SimpleNamespace())

        monkeypatch.setattr(lifecycle, "_LOGIN_PURGE_ENABLED", False)
        monkeypatch.setattr(lifecycle, "_FRESHNESS_SAMPLER_ENABLED", False)
        monkeypatch.setattr(lifecycle, "_FLEET_AUTO_REFRESH_ENABLED", False)
        monkeypatch.setattr(idx_state, "DATABASE_URL", "postgresql://fake/db")
        monkeypatch.setattr(idx_state, "_user_store", _FakeUserStore())
        monkeypatch.setattr(idx_state, "_jobs", {})
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")

        monkeypatch.setattr("treeweft.infrastructure.config.validate_config", lambda: None)
        monkeypatch.setattr("treeweft.infrastructure.logging.configure_logging", lambda **kw: None)
        monkeypatch.setattr("treeweft.infrastructure.tracing.init_tracer", lambda *a, **kw: None)

        async def fake_init_pool(dsn):
            return object()

        monkeypatch.setattr("treeweft.adapters.postgresql.connection.init_pool", fake_init_pool)

        async def fake_run_migrations():
            return []

        monkeypatch.setattr("treeweft.adapters.postgresql.run_migrations", fake_run_migrations)

        async def fake_ensure_schema():
            pass

        monkeypatch.setattr(lifecycle.graph_store, "ensure_schema", fake_ensure_schema)
        monkeypatch.setattr(
            "treeweft.adapters.postgresql.job_store.JobStore", lambda: job_store
        )
        monkeypatch.setattr(
            "treeweft.adapters.postgresql.job_group_store.JobGroupStore",
            lambda: _FakeLifecycleJobGroupStore(),
        )
        queue = _FakeLifecycleQueue()
        monkeypatch.setattr(
            "treeweft.adapters.queue.postgres_queue.PostgresJobQueue", lambda: queue
        )

        async def fake_run_check():
            pass

        monkeypatch.setattr(lifecycle.index_guard, "run_check", fake_run_check)
        monkeypatch.setattr(lifecycle.index_guard, "start_refresh_loop", lambda: None)

        async def fake_load_and_seed():
            pass

        async def fake_start_sync():
            pass

        monkeypatch.setattr(lifecycle.prompt_pins, "load_and_seed", fake_load_and_seed)
        monkeypatch.setattr(lifecycle.prompt_pins, "start_sync", fake_start_sync)

        async def fake_persist(jd):
            job_store.jobs[jd["id"]] = Job.from_dict(jd)

        monkeypatch.setattr(runners, "_persist_job", fake_persist)

        class _DummyProbe:
            def close(self):
                pass

        def fake_dispatch_job(job):
            idx_state._jobs[job.id] = job.to_dict()
            return _DummyProbe()

        monkeypatch.setattr(runners, "dispatch_job", fake_dispatch_job)

        await lifecycle.startup(app)
        return queue

    async def test_does_not_promote_follower_of_a_waiting_head(self, monkeypatch):
        """refresh-D is still RUNNING; wait-A legitimately waits on it and
        must not be promoted. wait-B waits on wait-A (still 'waiting') --
        recovery must not treat wait-A's 'waiting' status as orphaned and
        promote wait-B out from under it, or two jobs end up queued for the
        same source."""
        refresh = Job(id="refresh-D", kind="resummarize", source_id="src-D",
                      status=JobStatus.RUNNING)
        head = Job(id="wait-A", kind="incremental", source_id="src-D",
                   status=JobStatus.WAITING, payload={"after_job": "refresh-D"})
        follower = Job(id="wait-B", kind="incremental", source_id="src-D",
                       status=JobStatus.WAITING, payload={"after_job": "wait-A"})
        job_store = InMemoryJobStore([refresh, head, follower])

        queue = await self._run_startup(monkeypatch, job_store=job_store)

        assert "wait-B" not in queue.enqueued
        assert "wait-A" not in queue.enqueued
        assert (await job_store.get("wait-A")).status == JobStatus.WAITING
        assert (await job_store.get("wait-B")).status == JobStatus.WAITING


# ---------------------------------------------------------------------------
# F: metrics.queue_depth decremented once when a queued refresh is cancelled
# ---------------------------------------------------------------------------


class TestQueueDepthMetricOnCancel:
    async def test_queue_depth_decremented_once_on_cancel(self, monkeypatch):
        store = InMemoryJobStore([_refresh_job(status=JobStatus.QUEUED)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        from treeweft.infrastructure import metrics
        calls = {"n": 0}
        monkeypatch.setattr(metrics.queue_depth, "dec", lambda: calls.__setitem__("n", calls["n"] + 1))

        async def build():
            return {"job_id": "incoming-f", "id": "incoming-f", "kind": "incremental",
                    "status": "queued", "message": "m"}

        outcome = await prompt_refresh.preempt_active_refresh("src-1", build)

        assert outcome.mode == "queued"
        assert calls["n"] == 1

    async def test_queue_depth_not_decremented_when_no_active_refresh(self, monkeypatch):
        store = InMemoryJobStore([])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        from treeweft.infrastructure import metrics
        calls = {"n": 0}
        monkeypatch.setattr(metrics.queue_depth, "dec", lambda: calls.__setitem__("n", calls["n"] + 1))

        async def build():
            return {"job_id": "incoming-g", "id": "incoming-g", "kind": "incremental",
                    "status": "queued", "message": "m"}

        outcome = await prompt_refresh.preempt_active_refresh("src-1", build)

        assert outcome.mode == "queued"
        assert calls["n"] == 0
