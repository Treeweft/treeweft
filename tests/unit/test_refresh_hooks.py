"""Refresh-hook wiring for ADR-003 prompt versioning (research R4, R5; T023).

Three things are covered:

- `PostgresJobQueue._run_one` (`postgres_queue.py`) calls
  `prompt_refresh.enqueue_if_stale(job.source_id)` after every terminal
  status of a non-`resummarize` job -- a clean finish, a dispatch refusal,
  "cannot dispatch", and dead-lettering after exhausted retries -- but never
  after a `resummarize` job itself (no self-requeue loop) and never after a
  retryable job that gets re-queued for another attempt (not terminal). A
  hook failure is logged and never changes the finished job's status
  (constitution V).
- `lifecycle.startup()`'s recovery loop exempts `kind == "resummarize"` from
  the "superseded by prior completed index for this source" rule: an
  interrupted resummarize is re-enqueued like any job with no prior
  completion, while other kinds keep the superseded behaviour.
- `index_guard.dispatch_allowed` refuses a queued `resummarize` like any
  other non-rebuild job while the index is `reindex_required`.

`enqueue_if_stale`'s own current-source / active-job / not-writable
contract is exercised directly too (`TestEnqueueIfStaleContract`), since
`tests/unit/test_prompt_routes.py` only drives that logic indirectly
through the pin-change route's `plan_refreshes`/`enqueue_refreshes` path,
never `enqueue_if_stale` itself. The two-process race on
`jobs_active_source_uniq` is already covered by
`test_prompt_routes.py::TestRaceOnJobsActiveSourceUniq` and is not
duplicated here.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from treeweft.adapters.postgresql import maintenance_lock
from treeweft.adapters.queue.postgres_queue import PostgresJobQueue
from treeweft.application import index_guard as ig
from treeweft.application import indexer_runners as runners
from treeweft.application import indexer_state as idx_state
from treeweft.application import lifecycle
from treeweft.application import prompt_pins
from treeweft.application import prompt_refresh
from treeweft.domain.jobs import Job, JobStatus
from treeweft.domain.sources import SourceRecord

pytestmark = pytest.mark.asyncio


def _job(job_id="job-1", *, kind="repo", source_id="src-1", group_id=None,
         status=JobStatus.QUEUED, payload=None) -> Job:
    return Job(
        id=job_id, kind=kind, source_id=source_id, status=status, group_id=group_id,
        payload=payload,
    )


async def _probe_none():
    return None


# ---------------------------------------------------------------------------
# _run_one: the hook fires after every terminal status, never after
# resummarize, never after a retry-requeue, and a hook failure is isolated.
# ---------------------------------------------------------------------------

class TestRunOneRefreshHook:
    async def _run(self, monkeypatch, job, *, dispatch_allowed=True, coro_factory=None):
        q = PostgresJobQueue(max_workers=1)
        store = AsyncMock()
        store.get = AsyncMock(return_value=job)

        persisted: list[dict] = []

        async def fake_persist(jd):
            persisted.append(dict(jd))

        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(runners, "_persist_job", fake_persist)
        monkeypatch.setattr(ig, "dispatch_allowed", AsyncMock(return_value=dispatch_allowed))
        monkeypatch.setattr(ig, "refusal_detail", lambda job_id: "refused")

        if coro_factory is not None:
            monkeypatch.setattr(runners, "dispatch_job", coro_factory)

        await q._run_one(worker_id=0, job_id=job.id)
        return persisted

    async def test_hook_called_after_repo_job_completes(self, monkeypatch):
        job = _job(kind="repo", source_id="src-1")

        async def ok_coro():
            return None

        calls: list[str] = []

        async def fake_enqueue_if_stale(source_id):
            calls.append(source_id)
            return "new-job-1"

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)
        await self._run(monkeypatch, job, coro_factory=lambda j: ok_coro())

        assert calls == ["src-1"]

    async def test_hook_called_after_incremental_job_completes(self, monkeypatch):
        job = _job(kind="incremental", source_id="src-2")

        async def ok_coro():
            return None

        calls: list[str] = []

        async def fake_enqueue_if_stale(source_id):
            calls.append(source_id)
            return None

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)
        await self._run(monkeypatch, job, coro_factory=lambda j: ok_coro())

        assert calls == ["src-2"]

    async def test_hook_not_called_when_resummarize_finishes_at_current_target(
        self, monkeypatch
    ):
        """Fix 2: the effective version still equals the job's own target --
        the pin didn't move past what this refresh was already chasing, so
        re-enqueuing would just requeue the same failing refresh forever."""
        job = _job(kind="resummarize", source_id="src-3", payload={"target_version": 5})

        async def ok_coro():
            return None

        calls: list[str] = []
        reload_calls: list[int] = []

        async def fake_enqueue_if_stale(source_id):
            calls.append(source_id)
            return None

        async def fake_reload():
            reload_calls.append(1)

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)
        monkeypatch.setattr(prompt_pins, "reload", fake_reload)
        monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: 5)
        await self._run(monkeypatch, job, coro_factory=lambda j: ok_coro())

        assert reload_calls == [1]
        assert calls == []

    async def test_hook_called_when_resummarize_overtaken_by_pin_change(self, monkeypatch):
        """Fix 2: the pin moved away from the target this job was chasing --
        the source must not be left stranded on the abandoned target."""
        job = _job(kind="resummarize", source_id="src-3", payload={"target_version": 5})

        async def ok_coro():
            return None

        calls: list[str] = []

        async def fake_enqueue_if_stale(source_id):
            calls.append(source_id)
            return None

        async def fake_reload():
            return None

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)
        monkeypatch.setattr(prompt_pins, "reload", fake_reload)
        monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: 7)
        await self._run(monkeypatch, job, coro_factory=lambda j: ok_coro())

        assert calls == ["src-3"]

    async def test_hook_called_after_dispatch_refusal(self, monkeypatch):
        job = _job(kind="repo", source_id="src-4")

        calls: list[str] = []

        async def fake_enqueue_if_stale(source_id):
            calls.append(source_id)
            return None

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)
        persisted = await self._run(monkeypatch, job, dispatch_allowed=False)

        assert calls == ["src-4"]
        assert persisted[-1]["status"] == "failed"

    async def test_hook_called_after_cannot_dispatch(self, monkeypatch):
        job = _job(kind="repo", source_id="src-5")

        calls: list[str] = []

        async def fake_enqueue_if_stale(source_id):
            calls.append(source_id)
            return None

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)
        persisted = await self._run(monkeypatch, job, coro_factory=lambda j: None)

        assert calls == ["src-5"]
        assert persisted[-1]["status"] == "failed"

    async def test_hook_called_after_dead_letter(self, monkeypatch):
        job = _job(kind="incremental", source_id="src-6")

        async def boom_coro():
            raise RuntimeError("boom")

        calls: list[str] = []

        async def fake_enqueue_if_stale(source_id):
            calls.append(source_id)
            return None

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)
        monkeypatch.setattr(
            "treeweft.infrastructure.metrics.dead_letter_jobs_total",
            SimpleNamespace(inc=lambda: None),
        )
        monkeypatch.setattr(
            "treeweft.infrastructure.metrics.incremental_jobs_total",
            SimpleNamespace(labels=lambda **kw: SimpleNamespace(inc=lambda: None)),
        )

        q = PostgresJobQueue(max_workers=1)
        store = AsyncMock()
        store.get = AsyncMock(return_value=job)
        store.increment_attempts = AsyncMock(return_value=99)  # exceeds MAX_JOB_ATTEMPTS

        persisted: list[dict] = []

        async def fake_persist(jd):
            persisted.append(dict(jd))

        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(runners, "_persist_job", fake_persist)
        monkeypatch.setattr(runners, "dispatch_job", lambda j: boom_coro())
        monkeypatch.setattr(ig, "dispatch_allowed", AsyncMock(return_value=True))

        await q._run_one(worker_id=0, job_id=job.id)

        assert calls == ["src-6"]
        assert persisted[-1]["status"] == "dead_letter"

    async def test_hook_not_called_when_retryable_job_is_requeued(self, monkeypatch):
        """A retryable job's failed attempt that has not exhausted
        MAX_JOB_ATTEMPTS is re-queued, not terminal -- the hook must not
        fire, or a still-in-flight retry could race the refresh it starts."""
        job = _job(kind="incremental", source_id="src-7")

        async def boom_coro():
            raise RuntimeError("boom")

        calls: list[str] = []

        async def fake_enqueue_if_stale(source_id):
            calls.append(source_id)
            return None

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)

        q = PostgresJobQueue(max_workers=1)
        store = AsyncMock()
        store.get = AsyncMock(return_value=job)
        store.increment_attempts = AsyncMock(return_value=1)  # below MAX_JOB_ATTEMPTS

        persisted: list[dict] = []

        async def fake_persist(jd):
            persisted.append(dict(jd))

        enqueued: list[str] = []

        async def fake_enqueue(job_id):
            enqueued.append(job_id)

        monkeypatch.setattr(q, "enqueue", fake_enqueue)
        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(runners, "_persist_job", fake_persist)
        monkeypatch.setattr(runners, "dispatch_job", lambda j: boom_coro())
        monkeypatch.setattr(ig, "dispatch_allowed", AsyncMock(return_value=True))

        await q._run_one(worker_id=0, job_id=job.id)

        assert calls == []
        assert persisted[-1]["status"] == "queued"
        assert enqueued == [job.id]

    async def test_hook_raising_does_not_change_job_status_and_logs_error(
        self, monkeypatch, caplog
    ):
        job = _job(kind="repo", source_id="src-8")

        async def ok_coro():
            return None

        async def fake_enqueue_if_stale(source_id):
            raise RuntimeError("boom in hook")

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)

        with caplog.at_level(logging.ERROR):
            persisted = await self._run(monkeypatch, job, coro_factory=lambda j: ok_coro())

        # The runner's own terminal persist is the fake no-op coroutine
        # above (nothing further), so the last thing persisted is still the
        # "running" write from before dispatch -- the hook's failure adds
        # no persist call and does not touch the job.
        assert persisted[-1]["status"] == "running"
        assert any("src-8" in r.getMessage() for r in caplog.records)

    async def test_hook_not_called_when_job_has_no_source_id(self, monkeypatch):
        job = _job(kind="repo", source_id="")

        async def ok_coro():
            return None

        calls: list[str] = []

        async def fake_enqueue_if_stale(source_id):
            calls.append(source_id)
            return None

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)
        await self._run(monkeypatch, job, coro_factory=lambda j: ok_coro())

        assert calls == []

    async def test_hook_called_when_non_retryable_job_raises_out_of_its_runner(
        self, monkeypatch
    ):
        """Fix 6a: a kind outside `_RETRYABLE_KINDS` that raises past its own
        runner (defensive path -- normal runners catch internally and call
        `_fail_job`) is still a terminal outcome and must not skip the hook."""
        job = _job(kind="repo", source_id="src-10")

        async def boom_coro():
            raise RuntimeError("escaped the runner")

        calls: list[str] = []

        async def fake_enqueue_if_stale(source_id):
            calls.append(source_id)
            return None

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)
        persisted = await self._run(monkeypatch, job, coro_factory=lambda j: boom_coro())

        assert calls == ["src-10"]
        assert persisted[-1]["status"] == "failed"

    async def test_fail_job_persists_terminal_status_before_hook_runs(self, monkeypatch):
        """Fix 6b: `_fail_job` must await its persistence, not fire-and-
        forget it, so the hook never races a still-in-flight write -- or
        `enqueue_if_stale` could see this job as still active and refuse the
        replacement refresh."""
        job = _job(kind="repo", source_id="src-11")

        persisted: list[dict] = []

        async def fake_persist(jd):
            persisted.append(dict(jd))

        async def failing_coro():
            # Mirrors a runner's own except block: catches internally, calls
            # _fail_job, and returns normally (no exception escapes).
            await runners._fail_job(
                {"job_id": job.id, "source_id": job.source_id}, RuntimeError("boom"),
            )

        order: list[str] = []

        async def fake_enqueue_if_stale(source_id):
            order.append("hook")
            assert persisted and persisted[-1]["status"] == "failed"
            return None

        monkeypatch.setattr(prompt_refresh, "enqueue_if_stale", fake_enqueue_if_stale)

        q = PostgresJobQueue(max_workers=1)
        store = AsyncMock()
        store.get = AsyncMock(return_value=job)

        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(runners, "_persist_job", fake_persist)
        monkeypatch.setattr(ig, "dispatch_allowed", AsyncMock(return_value=True))
        monkeypatch.setattr(ig, "refusal_detail", lambda job_id: "refused")
        monkeypatch.setattr(runners, "dispatch_job", lambda j: failing_coro())

        await q._run_one(worker_id=0, job_id=job.id)

        assert order == ["hook"]
        assert persisted[-1]["status"] == "failed"


# ---------------------------------------------------------------------------
# enqueue_if_stale's own contract (not covered by test_prompt_routes.py,
# which only drives it indirectly through plan_refreshes/enqueue_refreshes).
# ---------------------------------------------------------------------------

class _FakeSourceRepo:
    def __init__(self, sources):
        self.by_id = {s.id: s for s in sources}

    async def get_by_id(self, source_id):
        return self.by_id.get(source_id)


class _FakeActiveJob:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return self._d


class _FakeRefreshJobStore:
    def __init__(self, active=None):
        self.active = active or {}
        self.upserted: list = []

    async def find_active_for_source(self, source_id):
        d = self.active.get(source_id)
        return _FakeActiveJob(d) if d else None

    async def upsert(self, job):
        self.upserted.append(job)


class _FakeRefreshQueue:
    def __init__(self):
        self.enqueued: list[str] = []

    async def enqueue(self, job_id):
        self.enqueued.append(job_id)


def _source(id_="src-1", *, version=3, target=None, chunk_count=10) -> SourceRecord:
    return SourceRecord(
        id=id_, path="", url="https://example/repo", branch="main",
        chunk_count=chunk_count, summary_prompt_version=version,
        summary_refresh_target=target,
    )


class TestEnqueueIfStaleContract:
    async def test_returns_none_when_source_is_current(self, monkeypatch):
        monkeypatch.setattr(idx_state, "_source_repo", _FakeSourceRepo([_source(version=3)]))
        monkeypatch.setattr(
            prompt_pins, "_view",
            prompt_pins.PinView(deployment={"chunk_summary": 3}, overrides={}),
        )

        assert await prompt_refresh.enqueue_if_stale("src-1") is None

    async def test_returns_none_when_another_job_is_active(self, monkeypatch):
        monkeypatch.setattr(idx_state, "_source_repo", _FakeSourceRepo([_source(version=2)]))
        monkeypatch.setattr(
            prompt_pins, "_view",
            prompt_pins.PinView(deployment={"chunk_summary": 3}, overrides={}),
        )
        monkeypatch.setattr(
            idx_state, "_job_store",
            _FakeRefreshJobStore(active={"src-1": {"job_id": "blocking-1"}}),
        )

        assert await prompt_refresh.enqueue_if_stale("src-1") is None

    async def test_returns_none_when_index_not_writable(self, monkeypatch):
        monkeypatch.setattr(idx_state, "_source_repo", _FakeSourceRepo([_source(version=2)]))
        monkeypatch.setattr(
            prompt_pins, "_view",
            prompt_pins.PinView(deployment={"chunk_summary": 3}, overrides={}),
        )
        monkeypatch.setattr(idx_state, "_job_store", _FakeRefreshJobStore())
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required", reason="stale"))

        assert await prompt_refresh.enqueue_if_stale("src-1") is None

    async def test_enqueues_when_stale_no_active_job_and_writable(self, monkeypatch):
        monkeypatch.setattr(idx_state, "_source_repo", _FakeSourceRepo([_source(version=2)]))
        monkeypatch.setattr(
            prompt_pins, "_view",
            prompt_pins.PinView(deployment={"chunk_summary": 3}, overrides={}),
        )
        job_store = _FakeRefreshJobStore()
        queue = _FakeRefreshQueue()
        monkeypatch.setattr(idx_state, "_job_store", job_store)
        monkeypatch.setattr(idx_state, "_job_group_store", None)
        monkeypatch.setattr(idx_state, "_job_queue", queue)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("ok"))

        job_id = await prompt_refresh.enqueue_if_stale("src-1")

        assert job_id is not None
        assert queue.enqueued == [job_id]
        assert job_store.upserted[0].kind == "resummarize"


# ---------------------------------------------------------------------------
# Authoritative read (finding #1): enqueue_if_stale must reload before
# deciding staleness, so a stale local view can't hide a pin move another
# process already committed.
# ---------------------------------------------------------------------------

def _reload_pin_row(operation: str, version: int):
    from datetime import datetime, timezone

    from treeweft.adapters.postgresql.prompt_pin_store import PinRow

    return PinRow(
        operation=operation, scope="deployment", version=version,
        updated_at=datetime.now(timezone.utc), updated_by=None,
    )


class _FakeReloadPinStore:
    def __init__(self, rows):
        self._rows = rows

    async def list_all(self):
        return list(self._rows)


class TestEnqueueIfStaleAuthoritativeRead:
    async def test_uses_stored_pin_not_stale_local_view(self, monkeypatch):
        """Postgres already holds chunk_summary=4 (a peer process moved the
        pin), but this process's local view is stale and still thinks it's
        at 3. The source is recorded at v3, so a stale-view read would
        wrongly call it current and skip the refresh; a fresh read must
        catch that it is stale toward v4."""
        from treeweft.adapters.llm_api import prompts

        monkeypatch.setitem(
            prompts.REGISTRY,
            "chunk_summary",
            {
                3: prompts.REGISTRY["chunk_summary"][3],
                4: prompts.PromptVersion(
                    operation="chunk_summary", version=4, system="v4",
                    schema=prompts.FrozenResponseSchema(min_length=1), notes="test v4",
                ),
            },
        )
        monkeypatch.setattr(idx_state, "_source_repo", _FakeSourceRepo([_source(version=3)]))
        monkeypatch.setattr(idx_state, "DATABASE_URL", "postgresql://fake")
        monkeypatch.setattr(
            prompt_pins, "_view",
            prompt_pins.PinView(deployment={"chunk_summary": 3, "hyde": 1}, overrides={}),
        )
        monkeypatch.setattr(
            prompt_pins, "_pin_store",
            _FakeReloadPinStore([_reload_pin_row("chunk_summary", 4), _reload_pin_row("hyde", 1)]),
        )
        job_store = _FakeRefreshJobStore()
        queue = _FakeRefreshQueue()
        monkeypatch.setattr(idx_state, "_job_store", job_store)
        monkeypatch.setattr(idx_state, "_job_group_store", None)
        monkeypatch.setattr(idx_state, "_job_queue", queue)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("ok"))

        job_id = await prompt_refresh.enqueue_if_stale("src-1")

        assert job_id is not None
        assert queue.enqueued == [job_id]
        assert prompt_pins.view().deployment["chunk_summary"] == 4


# ---------------------------------------------------------------------------
# dispatch_allowed refuses a queued resummarize like any other job while
# reindex_required -- there is no kind-based bypass.
# ---------------------------------------------------------------------------

class FakeJobGroupStore:
    def __init__(self, groups=None):
        self._groups = groups or {}

    async def latest_by_kind(self, kind: str):
        matching = [g for g in self._groups.values() if g.kind == kind]
        return max(matching, key=lambda g: g.created_at) if matching else None


class FakeJobStore:
    def __init__(self, jobs=None):
        self._jobs = list(jobs or [])

    async def list_by_group(self, group_id: str):
        return [j for j in self._jobs if j.group_id == group_id]


class TestDispatchAllowedRefusesResummarize:
    async def test_queued_resummarize_refused_while_reindex_required(self, monkeypatch):
        monkeypatch.setattr(idx_state, "_job_group_store", FakeJobGroupStore())
        monkeypatch.setattr(idx_state, "_job_store", FakeJobStore())
        monkeypatch.setattr(maintenance_lock, "probe", _probe_none)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required", reason="stale"))

        async def fake_recheck():
            return ig.IndexStatus("reindex_required", reason="still mismatched")

        monkeypatch.setattr(ig, "_recheck_stamps_cheap", fake_recheck)

        job = _job("resum-1", kind="resummarize", group_id=None)
        assert await ig.dispatch_allowed(job) is False
        assert "still mismatched" in ig.refusal_detail(job.id)


# ---------------------------------------------------------------------------
# Startup recovery exempts resummarize from the "superseded" rule.
# ---------------------------------------------------------------------------

class _FakeUserStore:
    async def count_users(self):
        return 1


class _FakeLifecycleJobStore:
    def __init__(self, *, done=None, running=None, queued=None):
        self._done = list(done or [])
        self._running = list(running or [])
        self._queued = list(queued or [])

    async def init(self):
        pass

    async def list_by_status(self, status):
        if status == JobStatus.DONE:
            return list(self._done)
        if status == JobStatus.RUNNING:
            return list(self._running)
        if status == JobStatus.QUEUED:
            return list(self._queued)
        return []


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


class TestLifecycleRecoveryResummarizeExemption:
    @pytest.fixture(autouse=True)
    def _restore_job_globals(self):
        """`startup()` assigns `_state._job_store`/`_job_group_store`/
        `_job_queue` directly at runtime, which `monkeypatch` does not
        track -- same rationale as `test_lifecycle_prompt_pins.py`."""
        job_store = idx_state._job_store
        job_group_store = idx_state._job_group_store
        job_queue = idx_state._job_queue
        yield
        idx_state._job_store = job_store
        idx_state._job_group_store = job_group_store
        idx_state._job_queue = job_queue

    async def _run_startup(self, monkeypatch, *, done, incomplete):
        app = SimpleNamespace(state=SimpleNamespace())

        monkeypatch.setattr(lifecycle, "_LOGIN_PURGE_ENABLED", False)
        monkeypatch.setattr(lifecycle, "_FRESHNESS_SAMPLER_ENABLED", False)
        monkeypatch.setattr(lifecycle, "_FLEET_AUTO_REFRESH_ENABLED", False)
        monkeypatch.setattr(idx_state, "DATABASE_URL", "postgresql://fake/db")
        monkeypatch.setattr(idx_state, "_user_store", _FakeUserStore())
        monkeypatch.setattr(idx_state, "_jobs", {})
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")

        monkeypatch.setattr(
            "treeweft.infrastructure.config.validate_config", lambda: None
        )
        monkeypatch.setattr(
            "treeweft.infrastructure.logging.configure_logging", lambda **kw: None
        )
        monkeypatch.setattr(
            "treeweft.infrastructure.tracing.init_tracer", lambda *a, **kw: None
        )

        async def fake_init_pool(dsn):
            return object()

        monkeypatch.setattr(
            "treeweft.adapters.postgresql.connection.init_pool", fake_init_pool
        )

        async def fake_run_migrations():
            return []

        monkeypatch.setattr(
            "treeweft.adapters.postgresql.run_migrations", fake_run_migrations
        )

        async def fake_ensure_schema():
            pass

        monkeypatch.setattr(lifecycle.graph_store, "ensure_schema", fake_ensure_schema)

        running = [j for j in incomplete if j.status == JobStatus.RUNNING]
        queued = [j for j in incomplete if j.status == JobStatus.QUEUED]
        job_store = _FakeLifecycleJobStore(done=done, running=running, queued=queued)
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

        persisted: list[dict] = []

        async def fake_persist(jd):
            persisted.append(dict(jd))

        monkeypatch.setattr(runners, "_persist_job", fake_persist)

        class _DummyProbe:
            def close(self):
                pass

        def fake_dispatch_job(job):
            # Mirrors dispatch_job's real side effect of populating
            # _state._jobs[job.id], which the resume branch writes into.
            idx_state._jobs[job.id] = job.to_dict()
            return _DummyProbe()

        monkeypatch.setattr(runners, "dispatch_job", fake_dispatch_job)

        await lifecycle.startup(app)
        return persisted, queue

    async def test_interrupted_resummarize_is_reenqueued_despite_done_source(
        self, monkeypatch
    ):
        done_job = Job(id="done-1", kind="repo", source_id="src-1", status=JobStatus.DONE)
        resum_job = Job(
            id="resum-1", kind="resummarize", source_id="src-1", status=JobStatus.RUNNING
        )

        persisted, queue = await self._run_startup(
            monkeypatch, done=[done_job], incomplete=[resum_job]
        )

        # Never marked superseded/done by the recovery loop.
        assert not any(p.get("id") == "resum-1" for p in persisted)
        assert "resum-1" in queue.enqueued

    async def test_other_kind_still_superseded_when_source_has_done_job(self, monkeypatch):
        done_job = Job(id="done-1", kind="repo", source_id="src-2", status=JobStatus.DONE)
        stale_job = Job(
            id="stale-1", kind="incremental", source_id="src-2", status=JobStatus.RUNNING
        )

        persisted, queue = await self._run_startup(
            monkeypatch, done=[done_job], incomplete=[stale_job]
        )

        superseded = [p for p in persisted if p.get("id") == "stale-1"]
        assert superseded, "expected the incremental job to be marked superseded"
        assert superseded[-1]["status"] == "done"
        assert "stale-1" not in queue.enqueued
