"""The authoritative dispatch gate (ADR-004 §3, research R5 §2).

Covers `PostgresJobQueue._run_one`'s persist-then-check order and
`index_guard.dispatch_allowed()`'s five ordered rules, with the job store,
job-group store and maintenance lock faked — no real Postgres.
"""
from unittest.mock import AsyncMock

import pytest

from treeweft.adapters.postgresql import maintenance_lock
from treeweft.adapters.queue.postgres_queue import PostgresJobQueue
from treeweft.application import index_guard as ig
from treeweft.application import indexer_runners as runners
from treeweft.application import indexer_state as idx_state
from treeweft.domain.jobs import Job, JobGroup, JobStatus

pytestmark = pytest.mark.asyncio


def _job(job_id="job-1", group_id=None, status=JobStatus.QUEUED) -> Job:
    return Job(id=job_id, kind="repo", source_id="src-1", status=status, group_id=group_id)


class FakeJobGroupStore:
    def __init__(self, groups: dict[str, JobGroup] | None = None):
        self._groups = groups or {}

    async def latest_by_kind(self, kind: str) -> JobGroup | None:
        matching = [g for g in self._groups.values() if g.kind == kind]
        return max(matching, key=lambda g: g.created_at) if matching else None


class FakeJobStore:
    def __init__(self, jobs: list[Job] | None = None):
        self._jobs = list(jobs or [])

    async def list_by_group(self, group_id: str) -> list[Job]:
        return [j for j in self._jobs if j.group_id == group_id]


async def _probe_none():
    return None


async def _probe_exclusive():
    return "exclusive"


class TestRunOnePersistsBeforeCheck:
    async def test_running_is_persisted_before_dispatch_allowed_is_awaited(self, monkeypatch):
        q = PostgresJobQueue(max_workers=1)
        job = _job()
        store = AsyncMock()
        store.get = AsyncMock(return_value=job)

        order: list[str] = []
        persisted: list[dict] = []

        async def fake_persist(jd):
            persisted.append(jd)
            order.append(f"persist:{jd['status']}")

        async def fake_dispatch_allowed(j):
            order.append("dispatch_allowed")
            return True

        async def fake_dispatch_job(j):
            order.append("dispatch_job")
            return None  # None => "cannot dispatch" path, exercised elsewhere

        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(runners, "_persist_job", fake_persist)
        monkeypatch.setattr(runners, "dispatch_job", fake_dispatch_job)
        monkeypatch.setattr(ig, "dispatch_allowed", fake_dispatch_allowed)

        await q._run_one(worker_id=0, job_id=job.id)

        assert order[0] == "persist:running"
        assert order.index("persist:running") < order.index("dispatch_allowed")
        assert persisted[0]["status"] == "running"

    async def test_refused_job_is_persisted_failed_and_dispatch_job_never_called(self, monkeypatch):
        q = PostgresJobQueue(max_workers=1)
        job = _job()
        store = AsyncMock()
        store.get = AsyncMock(return_value=job)

        persisted: list[dict] = []
        dispatch_job_called = []

        async def fake_persist(jd):
            persisted.append(jd)

        async def fake_dispatch_allowed(j):
            return False

        def fake_refusal_detail(job_id):
            return "Index requires rebuild: vector store (milvus): embedding_model mismatch."

        def fake_dispatch_job(j):
            dispatch_job_called.append(j)
            return None

        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(runners, "_persist_job", fake_persist)
        monkeypatch.setattr(runners, "dispatch_job", fake_dispatch_job)
        monkeypatch.setattr(ig, "dispatch_allowed", fake_dispatch_allowed)
        monkeypatch.setattr(ig, "refusal_detail", fake_refusal_detail)

        await q._run_one(worker_id=0, job_id=job.id)

        assert dispatch_job_called == []
        final = persisted[-1]
        assert final["status"] == "failed"
        assert "Index requires rebuild" in final["error"]

    async def test_attempts_are_never_incremented_on_refusal(self, monkeypatch):
        q = PostgresJobQueue(max_workers=1)
        job = _job()
        store = AsyncMock()
        store.get = AsyncMock(return_value=job)
        store.increment_attempts = AsyncMock(return_value=99)

        async def fake_persist(jd):
            pass

        monkeypatch.setattr(idx_state, "_job_store", store)
        monkeypatch.setattr(runners, "_persist_job", fake_persist)
        monkeypatch.setattr(ig, "dispatch_allowed", AsyncMock(return_value=False))
        monkeypatch.setattr(ig, "refusal_detail", lambda job_id: "refused")

        await q._run_one(worker_id=0, job_id=job.id)

        store.increment_attempts.assert_not_called()


class TestDispatchAllowedRules:
    async def test_rule1_rebuild_group_job_always_allowed_even_when_reindex_required(self, monkeypatch):
        group = JobGroup(id="grp-1", label="index rebuild", kind="index-rebuild", task_count=3)
        group_store = FakeJobGroupStore({group.id: group})
        job_store = FakeJobStore([_job("j1", group_id="grp-1", status=JobStatus.RUNNING)])
        monkeypatch.setattr(idx_state, "_job_group_store", group_store)
        monkeypatch.setattr(idx_state, "_job_store", job_store)
        monkeypatch.setattr(maintenance_lock, "probe", _probe_exclusive)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required", reason="stale"))

        job = _job("rebuild-job-1", group_id="grp-1")
        assert await ig.dispatch_allowed(job) is True

    async def test_rule2_refused_while_lock_held_exclusively(self, monkeypatch):
        monkeypatch.setattr(idx_state, "_job_group_store", FakeJobGroupStore())
        monkeypatch.setattr(idx_state, "_job_store", FakeJobStore())
        monkeypatch.setattr(maintenance_lock, "probe", _probe_exclusive)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("ok"))

        job = _job("other-job")
        allowed = await ig.dispatch_allowed(job)
        assert allowed is False
        assert "preparing" in ig.refusal_detail(job.id) or "rebuild" in ig.refusal_detail(job.id).lower()

    async def test_rule3_refused_when_latest_rebuild_interrupted(self, monkeypatch):
        group = JobGroup(id="grp-2", label="index rebuild", kind="index-rebuild", task_count=5)
        group_store = FakeJobGroupStore({group.id: group})
        job_store = FakeJobStore([_job("j1", group_id="grp-2", status=JobStatus.DONE)])  # 1 of 5, none active
        monkeypatch.setattr(idx_state, "_job_group_store", group_store)
        monkeypatch.setattr(idx_state, "_job_store", job_store)
        monkeypatch.setattr(maintenance_lock, "probe", _probe_none)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("ok"))

        job = _job("unrelated-job")
        allowed = await ig.dispatch_allowed(job)
        assert allowed is False
        assert "interrupted" in ig.refusal_detail(job.id)

    async def test_rule4_stale_cache_confirmed_still_mismatched_refuses(self, monkeypatch):
        monkeypatch.setattr(idx_state, "_job_group_store", FakeJobGroupStore())
        monkeypatch.setattr(idx_state, "_job_store", FakeJobStore())
        monkeypatch.setattr(maintenance_lock, "probe", _probe_none)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required", reason="stale"))

        async def fake_recheck():
            return ig.IndexStatus("reindex_required", reason="still mismatched")

        monkeypatch.setattr(ig, "_recheck_stamps_cheap", fake_recheck)

        job = _job("j-mismatch")
        allowed = await ig.dispatch_allowed(job)
        assert allowed is False
        assert "still mismatched" in ig.refusal_detail(job.id)

    async def test_rule4_stale_cache_now_matching_allows_and_updates_cache(self, monkeypatch):
        monkeypatch.setattr(idx_state, "_job_group_store", FakeJobGroupStore())
        monkeypatch.setattr(idx_state, "_job_store", FakeJobStore())
        monkeypatch.setattr(maintenance_lock, "probe", _probe_none)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required", reason="stale"))

        async def fake_recheck():
            return ig.IndexStatus("ok")

        monkeypatch.setattr(ig, "_recheck_stamps_cheap", fake_recheck)

        job = _job("j-now-ok")
        assert await ig.dispatch_allowed(job) is True

    async def test_rule5_otherwise_allowed(self, monkeypatch):
        monkeypatch.setattr(idx_state, "_job_group_store", FakeJobGroupStore())
        monkeypatch.setattr(idx_state, "_job_store", FakeJobStore())
        monkeypatch.setattr(maintenance_lock, "probe", _probe_none)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("ok"))

        job = _job("ordinary-job")
        assert await ig.dispatch_allowed(job) is True

    async def test_refusal_detail_defaults_when_not_set(self, monkeypatch):
        assert "rebuild" in ig.refusal_detail("never-refused-job-id").lower()

    async def test_refusal_details_are_per_job_no_cross_job_race(self, monkeypatch):
        monkeypatch.setattr(idx_state, "_job_group_store", FakeJobGroupStore())
        monkeypatch.setattr(idx_state, "_job_store", FakeJobStore())
        monkeypatch.setattr(maintenance_lock, "probe", _probe_exclusive)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("ok"))

        job_a = _job("job-a")
        job_b = _job("job-b")
        await ig.dispatch_allowed(job_a)
        await ig.dispatch_allowed(job_b)
        detail_a = ig.refusal_detail(job_a.id)
        detail_b = ig.refusal_detail(job_b.id)
        assert detail_a == detail_b  # both refused for the same reason
        # But each was tracked independently and consuming one doesn't affect the other.
        assert ig.refusal_detail(job_a.id) != detail_a  # already consumed -> default fallback
