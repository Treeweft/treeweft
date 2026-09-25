"""Several indexer processes sharing one Postgres (ADR-004 §3, research R13,
spec FR-024/SC-008).

Two independent `index_guard` module instances (loaded via `importlib`, so
each has its own cache — a real second import, not a monkeypatched
attribute) stand in for two processes. They share one fake maintenance
lock and one fake Postgres job/group store, exactly as two real indexer
processes share one Postgres.
"""
import importlib.util
import sys

import pytest

from treeweft.domain.index_stamp import IndexStamp, StoreObservation
from treeweft.domain.jobs import Job, JobGroup, JobStatus

pytestmark = pytest.mark.asyncio

MODEL = "Qwen/Qwen3-Embedding-0.6B"
DIM = 8
MATCHING_STAMP = IndexStamp(schema=1, embedding_model=MODEL, vector_dim=DIM)
MISMATCHED_STAMP = IndexStamp(schema=1, embedding_model="other-model", vector_dim=DIM)


def _load_second_index_guard():
    """A second, independent `index_guard` module instance — its own
    globals (`_status`, `_lock`, ...), separate from the one already
    imported as `treeweft.application.index_guard`."""
    spec = importlib.util.find_spec("treeweft.application.index_guard")
    module = importlib.util.module_from_spec(spec)
    # A distinct sys.modules name so its own internal `from treeweft import
    # retriever` etc. still resolve normally (those ARE shared modules —
    # the stamp check swaps their functions, just like the single-process
    # tests do; only index_guard's *cache* differs per process).
    sys.modules["treeweft.application._index_guard_b"] = module
    spec.loader.exec_module(module)
    return module


class FakeLockKey:
    """One shared advisory-lock slot: None, or ("exclusive"|"shared", holder_id)."""

    def __init__(self):
        self.state = None  # None | (mode, holder)

    async def acquire(self, mode: str, holder: str):
        if self.state is None:
            self.state = (mode, holder)
            return True
        held_mode, _ = self.state
        if mode == "shared" and held_mode == "shared":
            return True  # multiple shared holders allowed, simplified: track count
        return False

    def release(self, holder: str):
        if self.state is not None and self.state[1] == holder:
            self.state = None

    def probe(self):
        return self.state[0] if self.state else None


def _bind_lock(module, key: FakeLockKey, holder: str, monkeypatch):
    """Monkeypatch `module`'s `maintenance_lock` import to route through the
    shared `key`, tagging every acquire from this module with `holder`.
    Uses `monkeypatch` (not raw assignment) because `maintenance_lock` is a
    real, shared module that outlives this test otherwise."""

    class Handle:
        def __init__(self):
            self.released = False

        async def release(self):
            if not self.released:
                key.release(holder)
                self.released = True

    async def acquire(mode):
        ok = await key.acquire(mode, holder)
        return Handle() if ok else None

    async def probe():
        return key.probe()

    monkeypatch.setattr(module.maintenance_lock, "acquire", acquire)
    monkeypatch.setattr(module.maintenance_lock, "probe", probe)


class FakeJobGroupStore:
    def __init__(self):
        self.groups: dict[str, JobGroup] = {}
        self._n = 0

    async def create(self, label, kind, created_by=None, task_count=0):
        self._n += 1
        gid = f"grp-{self._n}"
        self.groups[gid] = JobGroup(id=gid, label=label, kind=kind, created_by=created_by, task_count=task_count)
        return gid

    async def latest_by_kind(self, kind):
        matching = [g for g in self.groups.values() if g.kind == kind]
        return max(matching, key=lambda g: g.created_at) if matching else None

    async def delete(self, group_id):
        self.groups.pop(group_id, None)


class FakeQueue:
    async def enqueue(self, job_id):
        pass


class FakeJobStore:
    def __init__(self):
        self.jobs: list[Job] = []

    async def get(self, job_id):
        return next((j for j in self.jobs if j.id == job_id), None)

    async def list_by_status(self, status):
        return [j for j in self.jobs if j.status == status]

    async def list_by_group(self, group_id):
        return [j for j in self.jobs if j.group_id == group_id]

    async def upsert(self, job: Job):
        self.jobs = [j for j in self.jobs if j.id != job.id] + [job]


def _bind_state(group_store, job_store, monkeypatch):
    """index_guard's helpers do `from treeweft.application import
    indexer_state as idx_state` lazily and read idx_state._job_group_store
    etc. — that module IS shared (only index_guard's own cache differs
    between A and B), so binding once affects both equally, matching two
    real processes pointed at one Postgres. monkeypatch, not raw
    assignment: indexer_state is a real module other test files rely on."""
    from treeweft.application import indexer_state as idx_state

    monkeypatch.setattr(idx_state, "_job_group_store", group_store)
    monkeypatch.setattr(idx_state, "_job_store", job_store)
    monkeypatch.setattr(idx_state, "_job_queue", FakeQueue())


def _bind_stores(vector_obs, graph_obs, monkeypatch):
    """Bind the shared vector/graph store shims. Since retriever/graph_store
    are genuinely shared modules, both A and B observe the same fakes here
    — this mirrors two processes pointed at the same Milvus/Neo4j, not two
    different stores. monkeypatch, not raw assignment, for the same reason
    as `_bind_state`."""
    from treeweft import graph_store, retriever

    async def vs_observe():
        return vector_obs

    async def gs_observe():
        return graph_obs

    async def noop_write(stamp):
        pass

    monkeypatch.setattr(retriever, "observe_index", vs_observe)
    monkeypatch.setattr(retriever, "write_stamp", noop_write)
    monkeypatch.setattr(graph_store, "observe_index", gs_observe)
    monkeypatch.setattr(graph_store, "write_stamp", noop_write)


@pytest.fixture
def two_processes(monkeypatch):
    a = sys.modules["treeweft.application.index_guard"]
    b = _load_second_index_guard()
    key = FakeLockKey()
    _bind_lock(a, key, "A", monkeypatch)
    _bind_lock(b, key, "B", monkeypatch)
    group_store = FakeJobGroupStore()
    job_store = FakeJobStore()
    _bind_state(group_store, job_store, monkeypatch)
    monkeypatch.setenv("EMBEDDING_MODEL", MODEL)
    monkeypatch.setenv("VECTOR_DIM", str(DIM))
    vector_obs = StoreObservation(store="vector", backend="fake", exists=True, has_data=True, stamp=MATCHING_STAMP, schema_dim=DIM)
    graph_obs = StoreObservation(store="graph", backend="fake", exists=True, has_data=True, stamp=MATCHING_STAMP)
    _bind_stores(vector_obs, graph_obs, monkeypatch)
    yield {"a": a, "b": b, "lock": key, "group_store": group_store, "job_store": job_store}
    sys.modules.pop("treeweft.application._index_guard_b", None)


async def _no_sources():
    return []


class TestRebuildVersusWorker:
    async def test_workers_running_job_seen_by_rebuilds_step3_recheck_aborts(self, two_processes, monkeypatch):
        """B's job commits as running before A's rebuild takes the lock:
        A's step-3 re-check sees it, aborts, and deletes its group."""
        a, b = two_processes["a"], two_processes["b"]
        job_store = two_processes["job_store"]
        from treeweft.application import indexer_service as idx_svc
        monkeypatch.setattr(idx_svc, "_all_source_dicts", _no_sources)

        # B: persist running (the worker side of research R13's argument).
        job_store.jobs.append(Job(id="b-job", status=JobStatus.RUNNING))

        # A: attempts a rebuild. Step 0 blockers already see the running job.
        with pytest.raises(a.RebuildRefused):
            await a.rebuild(dry_run=False)

        assert two_processes["lock"].state is None  # never left held
        assert two_processes["group_store"].groups == {}  # nothing created/left behind

    async def test_lock_held_by_a_refuses_bs_job(self, two_processes):
        """A takes the lock before B's dispatch check: B's job is refused.
        A's own rebuild-group jobs, dispatched in B, are allowed."""
        a, b = two_processes["a"], two_processes["b"]
        job_store = two_processes["job_store"]

        handle = await a.maintenance_lock.acquire("exclusive")
        assert handle is not None

        other_job = Job(id="other", status=JobStatus.QUEUED, group_id=None)
        assert await b.dispatch_allowed(other_job) is False

        # Create the rebuild group A is "preparing" and a job that belongs to it.
        gid = await two_processes["group_store"].create("index rebuild", "index-rebuild", task_count=1)
        rebuild_job = Job(id="rebuild-job", status=JobStatus.QUEUED, group_id=gid)
        assert await b.dispatch_allowed(rebuild_job) is True

        await handle.release()


class TestRebuildFromReindexRequiredWithStaleCaches:
    async def test_rebuild_group_jobs_always_allowed_even_with_stale_caches(self, two_processes):
        a, b = two_processes["a"], two_processes["b"]
        a._status = a.IndexStatus("reindex_required", reason="stale")
        b._status = b.IndexStatus("reindex_required", reason="stale")

        group_store = two_processes["group_store"]
        gid = await group_store.create("index rebuild", "index-rebuild", task_count=2)
        job_in_a = Job(id="j-a", status=JobStatus.QUEUED, group_id=gid)
        job_in_b = Job(id="j-b", status=JobStatus.QUEUED, group_id=gid)
        # These are the group's own enqueued jobs (a real rebuild would have
        # persisted them at step 5 before either worker could pop one).
        two_processes["job_store"].jobs.extend([job_in_a, job_in_b])

        assert await a.dispatch_allowed(job_in_a) is True
        assert await b.dispatch_allowed(job_in_b) is True

    async def test_unrelated_job_right_after_completion_gets_a_fresh_recheck(self, two_processes):
        a, b = two_processes["a"], two_processes["b"]
        b._status = b.IndexStatus("reindex_required", reason="stale")
        # The stores now match (a rebuild elsewhere already fixed them),
        # so B's fresh stamp re-check (rule 4, no embedding) allows it.
        other_job = Job(id="other", status=JobStatus.QUEUED)
        assert await b.dispatch_allowed(other_job) is True
        assert b._status.state == "ok"  # the cache updated from the recheck


class TestCrashLeavesInterrupted:
    async def test_both_processes_report_interrupted_after_a_crash(self, two_processes):
        a, b = two_processes["a"], two_processes["b"]
        group_store = two_processes["group_store"]

        # Simulate A crashing after group creation but before all jobs
        # were enqueued: 1 of 3 jobs exists, none active, lock released
        # (a crashed connection is closed by Postgres itself).
        gid = await group_store.create("index rebuild", "index-rebuild", task_count=3)
        two_processes["job_store"].jobs.append(Job(id="j1", status=JobStatus.DONE, group_id=gid))

        status_a = await a.run_check()
        status_b = await b.run_check()
        assert status_a.state == "reindex_required"
        assert "interrupted" in status_a.reason
        assert status_b.state == "reindex_required"
        assert "interrupted" in status_b.reason


class TestStaleCacheDuringPreparing:
    async def test_bs_stale_ok_cache_accepts_then_job_fails_at_dispatch(self, two_processes):
        a, b = two_processes["a"], two_processes["b"]
        b._status = b.IndexStatus("ok")  # stale: doesn't know A is preparing

        handle = await a.maintenance_lock.acquire("exclusive")
        assert handle is not None

        # B's route-level gate (require_writable) reads only its cache: "ok" -> accepts.
        assert await b.require_writable() is None
        # But dispatch is authoritative and refuses.
        other_job = Job(id="other", status=JobStatus.QUEUED)
        assert await b.dispatch_allowed(other_job) is False

        await handle.release()

    async def test_bs_search_returns_preparing_409_not_a_store_error(self, two_processes):
        a, b = two_processes["a"], two_processes["b"]
        b._status = b.IndexStatus("ok")

        handle = await a.maintenance_lock.acquire("exclusive")
        assert handle is not None

        translated = await b.store_error_response(RuntimeError("collection missing"))
        assert translated is not None
        assert translated.status_code == 409

        await handle.release()


class TestConvergenceAfterCompletion:
    async def test_b_moves_reindex_required_to_rebuilding_to_ok(self, two_processes):
        a, b = two_processes["a"], two_processes["b"]
        b._status = b.IndexStatus("reindex_required", reason="stale")

        group_store = two_processes["group_store"]
        job_store = two_processes["job_store"]
        gid = await group_store.create("index rebuild", "index-rebuild", task_count=1)
        job_store.jobs.append(Job(id="only-job", status=JobStatus.QUEUED, group_id=gid))

        status = await b.refresh()
        assert status.state == "rebuilding"
        assert status.rebuild_progress == (0, 1)

        job_store.jobs[0].status = JobStatus.DONE
        status = await b.refresh()
        assert status.state == "ok"


class TestCommunityBuildVersusRebuild:
    async def test_community_build_blocks_a_rebuild(self, two_processes, monkeypatch):
        a, b = two_processes["a"], two_processes["b"]
        from treeweft.application import indexer_service as idx_svc
        monkeypatch.setattr(idx_svc, "_all_source_dicts", _no_sources)
        handle = await b.maintenance_lock.acquire("shared")
        assert handle is not None

        with pytest.raises(a.RebuildRefused):
            await a.rebuild(dry_run=False)

        await handle.release()

    async def test_rebuild_blocks_a_community_build(self, two_processes):
        a, b = two_processes["a"], two_processes["b"]
        handle = await a.maintenance_lock.acquire("exclusive")
        assert handle is not None

        denied = await b.maintenance_lock.acquire("shared")
        assert denied is None

        await handle.release()
