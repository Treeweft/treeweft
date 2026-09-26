"""ADR-003 refresh preemption (code-review finding #6).

Index work must not be silently dropped while a `resummarize` (summary-only
refresh) job holds a source's one "active job" slot (`jobs_active_source_uniq`,
migration 008). Design: index work preempts the refresh; the refresh resumes
later through the existing post-job hook.

- Queued refresh -> atomically cancelled, incoming job created/enqueued as
  the route would today (`prompt_refresh.preempt_active_refresh`, case
  "queued"). A lost claim race (queued -> running between the check and the
  cancel) falls through to the running-refresh case.
- Running refresh -> the incoming job is persisted `status=waiting` with
  `payload["after_job"] = <refresh id>`, left OUT of job_queue (so
  jobs_active_source_uniq, which only covers queued/running, is never
  touched by it).
- The refresh checks for a waiting follower at every batch boundary and
  stops cleanly if one exists (`indexer_runners._run_resummarize_job`).
- `postgres_queue.PostgresJobQueue._maybe_enqueue_refresh` promotes waiting
  followers on every terminal status of the resummarize job, before the
  existing overtaken-pin check.
- Startup recovery promotes any orphaned waiting job (`lifecycle.startup`).
- Multiple-waiting rule (simplest correct option, chosen over chaining):
  only the newest waiting job is promoted; older ones are marked done
  "superseded by a newer request".

Fakes every store (JobStore, JobQueue, source repo) -- no real Postgres, per
the unit-test taxonomy. Call-site tests invoke the route coroutines directly
(as `test_refresh_hooks.py` calls `q._run_one` directly) rather than through
TestClient, since `treeweft.application.indexer_service` imports cleanly
without the Milvus/Neo4j test fixtures.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from treeweft.adapters.queue.postgres_queue import PostgresJobQueue
from treeweft.application import indexer_runners as runners
from treeweft.application import indexer_service as idx_svc
from treeweft.application import indexer_state as idx_state
from treeweft.application import index_guard as ig
from treeweft.application import lifecycle
from treeweft.application import prompt_refresh
from treeweft.application import routes_webhook as _webhook
from treeweft.domain.jobs import Job, JobStatus
from treeweft.domain.sources import SourceRecord

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class InMemoryJobStore:
    """A minimal in-memory stand-in for adapters.postgresql.job_store.JobStore
    covering exactly the methods refresh preemption touches."""

    def __init__(self, jobs=None):
        self.jobs: dict[str, Job] = {j.id: j for j in (jobs or [])}

    def seed(self, job: Job) -> None:
        self.jobs[job.id] = job

    async def init(self):
        pass

    async def get(self, job_id):
        return self.jobs.get(job_id)

    async def upsert(self, job: Job) -> None:
        self.jobs[job.id] = job

    async def find_active_for_source(self, source_id):
        candidates = [
            j for j in self.jobs.values()
            if j.source_id == source_id and j.status in (JobStatus.QUEUED, JobStatus.RUNNING)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda j: j.start_time)

    async def find_waiting_after(self, after_job_id):
        out = [
            j for j in self.jobs.values()
            if j.status == JobStatus.WAITING and (j.payload or {}).get("after_job") == after_job_id
        ]
        out.sort(key=lambda j: j.start_time)
        return out

    async def cancel_if_queued(self, job_id, message):
        j = self.jobs.get(job_id)
        if j is None or j.status != JobStatus.QUEUED:
            return False
        jd = j.to_dict()
        jd["status"] = "done"
        jd["message"] = message
        jd["finished_at"] = time.time()
        self.jobs[job_id] = Job.from_dict(jd)
        return True

    async def list_by_status(self, status):
        return [j for j in self.jobs.values() if j.status == status]


class InMemoryQueue:
    def __init__(self):
        self.enqueued: list[str] = []
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    async def enqueue(self, job_id):
        self.enqueued.append(job_id)


class FakeSourceRepo:
    def __init__(self, sources):
        self.by_id = {s.id: s for s in sources}

    async def get_by_id(self, source_id):
        return self.by_id.get(source_id)


def _refresh_job(job_id="refresh-1", *, source_id="src-1", status=JobStatus.QUEUED,
                  start_time=1.0) -> Job:
    return Job(
        id=job_id, kind="resummarize", source_id=source_id, status=status,
        start_time=start_time, payload={"target_version": 5},
    )


def _fake_request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


@pytest.fixture(autouse=True)
def _writable_index(monkeypatch):
    monkeypatch.setattr(ig, "_status", ig.IndexStatus("ok"))


@pytest.fixture(autouse=True)
def _no_job_group_store(monkeypatch):
    monkeypatch.setattr(idx_state, "_job_group_store", None)


# ---------------------------------------------------------------------------
# preempt_active_refresh: the three cases
# ---------------------------------------------------------------------------


class TestPreemptActiveRefresh:
    async def test_queued_refresh_is_cancelled_and_incoming_job_enqueued(self, monkeypatch):
        store = InMemoryJobStore([_refresh_job(status=JobStatus.QUEUED)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        async def build():
            return {"job_id": "incoming-1", "id": "incoming-1", "kind": "incremental",
                    "status": "queued", "message": "Incremental index for 3 changed files"}

        outcome = await prompt_refresh.preempt_active_refresh("src-1", build)

        assert outcome.mode == "queued"
        assert outcome.preempted_job_id == "refresh-1"
        assert outcome.job["job_id"] == "incoming-1"
        assert queue.enqueued == ["incoming-1"]
        # The cancelled refresh is terminal, off the queue, and carries the
        # 'preempted by <kind> job' message.
        refresh = await store.get("refresh-1")
        assert refresh.status == JobStatus.DONE
        assert refresh.message == "preempted by incremental job"

    async def test_running_refresh_creates_waiting_job_not_in_queue(self, monkeypatch):
        store = InMemoryJobStore([_refresh_job(status=JobStatus.RUNNING)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        async def build():
            return {"job_id": "incoming-2", "id": "incoming-2", "kind": "incremental",
                    "status": "queued", "message": "Incremental index for 3 changed files"}

        outcome = await prompt_refresh.preempt_active_refresh("src-1", build)

        assert outcome.mode == "waiting"
        assert outcome.preempted_job_id == "refresh-1"
        assert outcome.job["status"] == "waiting"
        assert outcome.job["message"] == "queued after the running summary refresh"
        assert outcome.job["payload"]["after_job"] == "refresh-1"
        assert queue.enqueued == []  # never touches job_queue

        persisted = await store.get("incoming-2")
        assert persisted.status == JobStatus.WAITING
        # jobs_active_source_uniq only covers queued/running -- a waiting job
        # must never show up as this source's "active" job.
        active = await store.find_active_for_source("src-1")
        assert active.id == "refresh-1"

    async def test_lost_claim_race_falls_through_to_waiting(self, monkeypatch):
        """The route's outer check saw 'queued', but a worker claimed the
        refresh (queued -> running) before our cancel_if_queued landed."""
        store = InMemoryJobStore([_refresh_job(status=JobStatus.QUEUED)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        real_cancel = store.cancel_if_queued

        async def racy_cancel(job_id, message):
            # Simulate the worker's claim landing first.
            store.jobs[job_id] = Job.from_dict(
                {**store.jobs[job_id].to_dict(), "status": "running"}
            )
            return await real_cancel(job_id, message)

        monkeypatch.setattr(store, "cancel_if_queued", racy_cancel)

        async def build():
            return {"job_id": "incoming-3", "id": "incoming-3", "kind": "incremental",
                    "status": "queued", "message": "Incremental index for 3 changed files"}

        outcome = await prompt_refresh.preempt_active_refresh("src-1", build)

        assert outcome.mode == "waiting"
        assert queue.enqueued == []

    async def test_no_active_resummarize_falls_back_to_plain_enqueue(self, monkeypatch):
        """The refresh finished (or was never a resummarize) between the
        caller's own check and this call -- nothing to preempt."""
        store = InMemoryJobStore([])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        async def build():
            return {"job_id": "incoming-4", "id": "incoming-4", "kind": "incremental",
                    "status": "queued", "message": "Incremental index for 3 changed files"}

        outcome = await prompt_refresh.preempt_active_refresh("src-1", build)

        assert outcome.mode == "queued"
        assert outcome.preempted_job_id is None
        assert queue.enqueued == ["incoming-4"]


# ---------------------------------------------------------------------------
# promote_waiting_after: promotion + the multiple-waiting rule
# ---------------------------------------------------------------------------


class TestPromoteWaitingAfter:
    async def test_no_waiting_jobs_is_a_noop(self, monkeypatch):
        store = InMemoryJobStore([])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        assert await prompt_refresh.promote_waiting_after("refresh-1") == []
        assert queue.enqueued == []

    async def test_single_waiting_job_is_promoted(self, monkeypatch):
        waiting = Job(
            id="wait-1", kind="incremental", source_id="src-1", status=JobStatus.WAITING,
            start_time=10.0, payload={"after_job": "refresh-1"},
        )
        store = InMemoryJobStore([waiting])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        promoted = await prompt_refresh.promote_waiting_after("refresh-1")

        assert promoted == ["wait-1"]
        assert queue.enqueued == ["wait-1"]
        job = await store.get("wait-1")
        assert job.status == JobStatus.QUEUED

    async def test_multiple_waiting_jobs_are_chained_oldest_first(self, monkeypatch):
        # Superseding would lose the older push's changed files; chain instead.
        older = Job(
            id="wait-old", kind="incremental", source_id="src-1", status=JobStatus.WAITING,
            start_time=10.0, payload={"after_job": "refresh-1", "changed": ["a.py"]},
        )
        newer = Job(
            id="wait-new", kind="incremental", source_id="src-1", status=JobStatus.WAITING,
            start_time=20.0, payload={"after_job": "refresh-1", "changed": ["b.py"]},
        )
        store = InMemoryJobStore([older, newer])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        assert await prompt_refresh.promote_waiting_after("refresh-1") == ["wait-old"]
        assert queue.enqueued == ["wait-old"]
        assert (await store.get("wait-old")).status == JobStatus.QUEUED
        chained = await store.get("wait-new")
        assert chained.status == JobStatus.WAITING
        assert chained.payload["after_job"] == "wait-old"
        assert chained.payload["changed"] == ["b.py"]

        assert await prompt_refresh.promote_waiting_after("wait-old") == ["wait-new"]
        assert queue.enqueued == ["wait-old", "wait-new"]
        assert (await store.get("wait-new")).status == JobStatus.QUEUED


# ---------------------------------------------------------------------------
# postgres_queue._maybe_enqueue_refresh: promotion wiring
# ---------------------------------------------------------------------------


class TestMaybeEnqueueRefreshPromotion:
    @pytest.mark.parametrize("final_status", ["done", "failed", "dead_letter"])
    async def test_promotion_fires_on_every_terminal_status(self, monkeypatch, final_status):
        refresh = Job(
            id="refresh-9", kind="resummarize", source_id="src-9",
            status=JobStatus(final_status), payload={"target_version": 5},
        )
        waiting = Job(
            id="wait-9", kind="incremental", source_id="src-9", status=JobStatus.WAITING,
            payload={"after_job": "refresh-9"},
        )
        store = InMemoryJobStore([refresh, waiting])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        from treeweft.application import prompt_pins
        monkeypatch.setattr(prompt_pins, "reload", lambda: _noop())
        monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: 5)

        q = PostgresJobQueue(max_workers=1)
        await q._maybe_enqueue_refresh(0, refresh)

        assert queue.enqueued == ["wait-9"]
        promoted = await store.get("wait-9")
        assert promoted.status == JobStatus.QUEUED

    async def test_promotion_fires_after_a_non_refresh_job(self, monkeypatch):
        # A chained waiting job waits on an index job, not the refresh.
        finished = Job(id="wait-old", kind="incremental", source_id="src-11", status=JobStatus.DONE)
        chained = Job(
            id="wait-new", kind="incremental", source_id="src-11", status=JobStatus.WAITING,
            payload={"after_job": "wait-old"},
        )
        store = InMemoryJobStore([finished, chained])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)
        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", lambda sid: _noop())

        await PostgresJobQueue(max_workers=1)._maybe_enqueue_refresh(0, finished)

        assert queue.enqueued == ["wait-new"]
        assert (await store.get("wait-new")).status == JobStatus.QUEUED

    async def test_promotion_happens_before_overtaken_pin_check(self, monkeypatch):
        """Promoting first means the overtaken-check's enqueue_if_stale sees
        an active (now-queued) job for the source and defers -- proven here
        by asserting enqueue_if_stale is never called even though the pin
        moved past the refresh's own target."""
        refresh = Job(
            id="refresh-10", kind="resummarize", source_id="src-10",
            status=JobStatus.DONE, payload={"target_version": 5},
        )
        waiting = Job(
            id="wait-10", kind="incremental", source_id="src-10", status=JobStatus.WAITING,
            payload={"after_job": "refresh-10"},
        )
        store = InMemoryJobStore([refresh, waiting])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        from treeweft.application import prompt_pins
        monkeypatch.setattr(prompt_pins, "reload", lambda: _noop())
        # Pin moved past the refresh's own target=5 -- would normally trigger
        # the overtaken-check's enqueue_if_stale.
        monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: 7)

        calls: list[str] = []

        async def spy_enqueue_if_stale(source_id):
            calls.append(source_id)
            return None

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", spy_enqueue_if_stale)

        q = PostgresJobQueue(max_workers=1)
        await q._maybe_enqueue_refresh(0, refresh)

        # Promotion queued the waiting job first...
        assert queue.enqueued == ["wait-10"]
        # ...and the module patched here IS what postgres_queue re-imports
        # (it does `from treeweft.application import prompt_refresh` fresh
        # inside the function), so the spy would have recorded a call had
        # the overtaken-check fired.

    async def test_hook_reenqueues_refresh_after_promoted_job_finishes_when_stale(
        self, monkeypatch
    ):
        """End-to-end: promote a waiting job, then simulate ITS OWN terminal
        status -- the generic (non-resummarize) branch's enqueue_if_stale
        hook must fire for it, exactly as for any other finished job."""
        refresh = Job(
            id="refresh-11", kind="resummarize", source_id="src-11",
            status=JobStatus.DONE, payload={"target_version": 5},
        )
        waiting = Job(
            id="wait-11", kind="incremental", source_id="src-11", status=JobStatus.WAITING,
            payload={"after_job": "refresh-11"},
        )
        store = InMemoryJobStore([refresh, waiting])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        from treeweft.application import prompt_pins
        monkeypatch.setattr(prompt_pins, "reload", lambda: _noop())
        monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: 5)

        q = PostgresJobQueue(max_workers=1)
        await q._maybe_enqueue_refresh(0, refresh)
        assert queue.enqueued == ["wait-11"]

        # Now the promoted job itself finishes.
        promoted = await store.get("wait-11")
        calls: list[str] = []

        async def spy_enqueue_if_stale(source_id):
            calls.append(source_id)
            return "new-refresh"

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", spy_enqueue_if_stale)
        await q._maybe_enqueue_refresh(0, promoted)

        assert calls == ["src-11"]


async def _noop():
    return None


# ---------------------------------------------------------------------------
# Batch-boundary preemption in the resummarize runner
# ---------------------------------------------------------------------------


class _FakeSourceRepoForRunner:
    def __init__(self):
        self.recorded: list[tuple] = []
        self.marked: list[tuple] = []

    async def get_by_id(self, source_id):
        return SimpleNamespace(
            id=source_id, summary_prompt_version=3, summary_refresh_target=None,
        )

    async def mark_summary_refresh(self, source_id, target):
        self.marked.append((source_id, target))

    async def record_summary_version(self, source_id, version):
        self.recorded.append((source_id, version))


class _FakeRetrieverStore:
    def __init__(self, n_rows: int):
        self.rows = {
            i: {"id": i, "chunk_text": f"chunk-{i}", "file_path": f"f{i}.py", "language": "python"}
            for i in range(1, n_rows + 1)
        }
        self.write_batches: list[list] = []

    async def snapshot_source_row_ids(self, source_id):
        return list(self.rows.keys())

    async def fetch_rows(self, ids):
        return [dict(self.rows[i]) for i in ids]

    async def write_summary_vectors(self, rows, vectors):
        self.write_batches.append([r["id"] for r in rows])

    async def count_source_rows(self, source_id):
        return len(self.rows)


class TestBatchBoundaryPreemption:
    async def test_stops_cleanly_when_waiting_job_arrives(self, monkeypatch):
        import treeweft.retriever as retriever_module
        from treeweft.application import prompt_pins

        repo = _FakeSourceRepoForRunner()
        store = _FakeRetrieverStore(n_rows=6)
        monkeypatch.setattr(idx_state, "_source_repo", repo)
        monkeypatch.setattr(runners, "USE_SUMMARY_VECTOR", True)
        monkeypatch.setattr(runners, "summary_vectors_supported", lambda: True)
        monkeypatch.setattr(runners, "EMBED_BATCH_SIZE", 2)
        monkeypatch.setattr(prompt_pins, "reload", lambda: _noop())
        monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: 5)
        monkeypatch.setattr(
            retriever_module, "snapshot_source_row_ids", store.snapshot_source_row_ids, raising=False,
        )
        monkeypatch.setattr(retriever_module, "fetch_rows", store.fetch_rows, raising=False)
        monkeypatch.setattr(
            retriever_module, "write_summary_vectors", store.write_summary_vectors, raising=False,
        )
        monkeypatch.setattr(
            retriever_module, "count_source_rows", store.count_source_rows, raising=False,
        )
        monkeypatch.setattr(runners, "embed", lambda texts: _embed(texts))
        monkeypatch.setattr(runners.llm, "cache_get_many", lambda *a, **kw: _cache_get_many())
        monkeypatch.setattr(
            runners.llm, "summarize_with_cache",
            lambda text, language, file_path, *, version: _summarize(text),
        )

        job_store = InMemoryJobStore()
        monkeypatch.setattr(idx_state, "_job_store", job_store)

        # A waiting job (payload.after_job = this resummarize job's id)
        # arrives after the first batch -- simulated by seeding it now:
        # find_waiting_after is checked at the TOP of every batch, so it
        # is already visible before batch 1 starts. To exercise "after N
        # chunks" rather than zero, seed it only once the fake store has
        # processed batch 1: monkeypatch find_waiting_after to return
        # empty for the first call, then the real (seeded) result after.
        calls = {"n": 0}
        real_find = job_store.find_waiting_after

        async def find_waiting_after_after_first_batch(after_job_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return []
            return await real_find(after_job_id)

        monkeypatch.setattr(job_store, "find_waiting_after", find_waiting_after_after_first_batch)

        job = {
            "job_id": "resum-1", "id": "resum-1", "source_id": "src-1", "kind": "resummarize",
            "status": "queued", "errors": 0, "payload": {"target_version": 5},
            "total_files": 0, "processed_files": 0, "total_chunks": 0, "message": "",
        }

        job_store.seed(Job(
            id="wait-x", kind="incremental", source_id="src-1", status=JobStatus.WAITING,
            payload={"after_job": "resum-1"},
        ))

        await runners._run_resummarize_job(job)

        assert job["status"] == "done"
        assert "preempted by index work after" in job["message"]
        assert "2/6 chunks" in job["message"]
        # Only the first batch (2 rows) was written.
        assert store.write_batches == [[1, 2]]
        # The mark stays set (source stays stale) and version was never advanced.
        assert repo.marked == [("src-1", 5)]
        assert repo.recorded == []


async def _embed(texts):
    return [[0.1, 0.2] for _ in texts]


async def _cache_get_many(*a, **kw):
    return {}


async def _summarize(text):
    return f"Summary: {text}", "generated"


# ---------------------------------------------------------------------------
# Startup recovery promotes an orphaned waiting job
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


class TestLifecycleRecoveryPromotesOrphanedWaiting:
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

    async def test_promotes_waiting_job_whose_refresh_already_finished(self, monkeypatch):
        """The refresh finished (e.g. it was superseded/abandoned by the
        RUNNING/QUEUED recovery loop, or simply finished before the crash)
        so its status is terminal by the time we get here -- the waiting
        job must be promoted, not left stranded forever."""
        finished_refresh = Job(
            id="refresh-done", kind="resummarize", source_id="src-1", status=JobStatus.DONE,
        )
        waiting = Job(
            id="wait-1", kind="incremental", source_id="src-1", status=JobStatus.WAITING,
            payload={"after_job": "refresh-done"},
        )
        job_store = InMemoryJobStore([finished_refresh, waiting])

        queue = await self._run_startup(monkeypatch, job_store=job_store)

        assert "wait-1" in queue.enqueued
        promoted = await job_store.get("wait-1")
        assert promoted.status == JobStatus.QUEUED

    async def test_keeps_waiting_job_whose_refresh_was_resumed(self, monkeypatch):
        """The refresh was RUNNING when the indexer died and gets resumed
        under the SAME job id by the RUNNING/QUEUED recovery loop above --
        the waiting job must stay linked to it, untouched."""
        resumed_refresh = Job(
            id="refresh-running", kind="resummarize", source_id="src-2", status=JobStatus.RUNNING,
        )
        waiting = Job(
            id="wait-2", kind="incremental", source_id="src-2", status=JobStatus.WAITING,
            payload={"after_job": "refresh-running"},
        )
        job_store = InMemoryJobStore([resumed_refresh, waiting])

        queue = await self._run_startup(monkeypatch, job_store=job_store)

        assert "wait-2" not in queue.enqueued
        still_waiting = await job_store.get("wait-2")
        assert still_waiting.status == JobStatus.WAITING


# ---------------------------------------------------------------------------
# Call sites: queued refresh cancelled and index job enqueued
# ---------------------------------------------------------------------------


class TestCallSitePreemption:
    async def test_webhook_incremental(self, monkeypatch):
        store = InMemoryJobStore([_refresh_job(source_id="src-web", status=JobStatus.QUEUED)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        from treeweft import graph_store as gs

        async def fake_list_sources():
            return [{"id": "src-web", "url": "https://example/repo.git", "branch": "main"}]

        monkeypatch.setattr(gs, "list_sources", fake_list_sources)
        monkeypatch.setattr(_webhook, "make_source_id", lambda url, branch: "src-web")

        async def fake_handle(self, headers, body, raw_body=None):
            from treeweft.domain.webhook import Provider, WebhookPayload
            return WebhookPayload(
                provider=Provider.GITHUB,
                repo_url="https://example/repo.git",
                branch="main",
                changed_files=[{"path": "a.py", "action": "modified"}],
            )

        monkeypatch.setattr(
            "treeweft.adapters.webhook.github.GitHubAdapter.handle_webhook", fake_handle,
        )
        monkeypatch.setenv("WEBHOOK_GITHUB_SECRET", "s")

        from fastapi import Request

        async def _receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        scope = {
            "type": "http", "method": "POST", "path": "/webhook",
            "headers": [(b"x-github-event", b"pull_request")], "query_string": b"",
        }
        request = Request(scope, _receive)

        result = await _webhook.handle_webhook(request)

        assert result["status"] == "queued"
        assert queue.enqueued == [result["job_id"]]
        refresh = await store.get("refresh-1")
        assert refresh.status == JobStatus.DONE
        assert refresh.message == "preempted by incremental job"

    async def test_index_file(self, monkeypatch, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        source_id = None

        from treeweft.sources import make_source_id
        source_id = make_source_id(path=str(f))

        store = InMemoryJobStore([_refresh_job(source_id=source_id, status=JobStatus.QUEUED)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        req = idx_svc.IndexFileRequest(file_path=str(f))
        ack = await idx_svc.handle_index_file(req, _fake_request())

        assert ack.status == "queued"
        assert queue.enqueued == [ack.job_id]
        refresh = await store.get("refresh-1")
        assert refresh.status == JobStatus.DONE
        assert refresh.message == "preempted by file job"

    async def test_index_directory(self, monkeypatch, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")

        from treeweft.sources import make_source_id
        source_id = make_source_id(path=str(tmp_path))

        store = InMemoryJobStore([_refresh_job(source_id=source_id, status=JobStatus.QUEUED)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        req = idx_svc.IndexDirectoryRequest(directory=str(tmp_path), auto_preflight=False)
        ack = await idx_svc.handle_index_directory(req, _fake_request())

        assert ack.status == "queued"
        assert queue.enqueued == [ack.job_id]
        refresh = await store.get("refresh-1")
        assert refresh.status == JobStatus.DONE
        assert refresh.message == "preempted by directory job"

    async def test_index_repo(self, monkeypatch):
        from treeweft.sources import make_source_id
        source_id = make_source_id(path=None, url="https://example/repo.git", branch="main")

        store = InMemoryJobStore([_refresh_job(source_id=source_id, status=JobStatus.QUEUED)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        req = idx_svc.IndexRepoRequest(url="https://example/repo.git", branch="main")
        ack = await idx_svc.handle_index_repo(req, _fake_request())

        assert ack.status == "queued"
        assert queue.enqueued == [ack.job_id]
        refresh = await store.get("refresh-1")
        assert refresh.status == JobStatus.DONE
        assert refresh.message == "preempted by repo job"

    async def test_index_graph(self, monkeypatch):
        from treeweft.application import indexer_authz
        source_id = "src-graph"
        store = InMemoryJobStore([_refresh_job(source_id=source_id, status=JobStatus.QUEUED)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)
        monkeypatch.setattr(idx_state, "_source_repo", FakeSourceRepo([
            SimpleNamespace(id=source_id, url="", path="/repo", branch=""),
        ]))
        monkeypatch.setattr(indexer_authz, "_require_scope", lambda *a, **k: None)

        async def _allow(*a, **k):
            return None

        monkeypatch.setattr(indexer_authz, "_authorize_scope", _allow)

        req = idx_svc.IndexGraphRequest(source_id=source_id)
        ack = await idx_svc.handle_index_graph(req, _fake_request())

        assert ack.status == "queued"
        assert queue.enqueued == [ack.job_id]
        refresh = await store.get("refresh-1")
        assert refresh.status == JobStatus.DONE
        assert refresh.message == "preempted by graph job"

    async def test_fleet_reindex(self, monkeypatch):
        source_id = "src-fleet"
        store = InMemoryJobStore([_refresh_job(source_id=source_id, status=JobStatus.QUEUED)])
        queue = InMemoryQueue()
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(idx_state, "_job_queue", queue)

        src = {"id": source_id, "url": "https://example/fleet.git", "branch": "main", "kind": "repo"}
        active = await _webhook._find_active_job_for_source(source_id)
        assert active is not None and active["kind"] == "resummarize"

        async def build():
            return idx_svc._build_source_reindex_job(src)

        outcome = await prompt_refresh.preempt_active_refresh(source_id, build)

        assert outcome.mode == "queued"
        assert queue.enqueued == [outcome.job["job_id"]]
        refresh = await store.get("refresh-1")
        assert refresh.status == JobStatus.DONE
        assert refresh.message == "preempted by repo job"


# ---------------------------------------------------------------------------
# Non-resummarize active jobs: today's dedup response is unchanged
# ---------------------------------------------------------------------------


class TestNonResummarizeDedupUnchanged:
    def _active_repo_job(self, source_id) -> Job:
        return Job(id="active-repo-1", kind="repo", source_id=source_id, status=JobStatus.RUNNING)

    async def test_index_file_dedup_unchanged(self, monkeypatch, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        from treeweft.sources import make_source_id
        source_id = make_source_id(path=str(f))

        store = InMemoryJobStore([self._active_repo_job(source_id)])
        monkeypatch.setattr(idx_state, "_job_store", store)

        req = idx_svc.IndexFileRequest(file_path=str(f))
        ack = await idx_svc.handle_index_file(req, _fake_request())

        assert ack.job_id == "active-repo-1"
        assert ack.status == "running"

    async def test_index_directory_dedup_unchanged(self, monkeypatch, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        from treeweft.sources import make_source_id
        source_id = make_source_id(path=str(tmp_path))

        store = InMemoryJobStore([self._active_repo_job(source_id)])
        monkeypatch.setattr(idx_state, "_job_store", store)

        req = idx_svc.IndexDirectoryRequest(directory=str(tmp_path), auto_preflight=False)
        ack = await idx_svc.handle_index_directory(req, _fake_request())

        assert ack.job_id == "active-repo-1"
        assert ack.status == "running"

    async def test_index_repo_dedup_unchanged(self, monkeypatch):
        from treeweft.sources import make_source_id
        source_id = make_source_id(path=None, url="https://example/repo2.git", branch="main")

        store = InMemoryJobStore([self._active_repo_job(source_id)])
        monkeypatch.setattr(idx_state, "_job_store", store)

        req = idx_svc.IndexRepoRequest(url="https://example/repo2.git", branch="main")
        ack = await idx_svc.handle_index_repo(req, _fake_request())

        assert ack.job_id == "active-repo-1"
        assert ack.status == "running"

    async def test_fleet_dedup_unchanged(self, monkeypatch):
        source_id = "src-fleet-2"
        store = InMemoryJobStore([self._active_repo_job(source_id)])
        monkeypatch.setattr(idx_state, "_job_store", store)

        active = await _webhook._find_active_job_for_source(source_id)
        assert active is not None and active.get("kind") != "resummarize"
