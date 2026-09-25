"""The index rebuild (ADR-004 §3, research R7): dry run, the real call,
progress, failures and blockers. Fakes every store and Postgres access —
no real Milvus/Neo4j/Postgres.
"""
import pytest

from treeweft import graph_store, retriever
from treeweft.adapters.postgresql import maintenance_lock
from treeweft.application import index_guard as ig
from treeweft.application import indexer_runners as runners
from treeweft.application import indexer_service as idx_svc
from treeweft.application import indexer_state as idx_state
from treeweft.application import retrieval
from treeweft.domain.index_stamp import IndexStamp, StoreObservation
from treeweft.domain.jobs import Job, JobGroup, JobStatus

pytestmark = pytest.mark.asyncio

MODEL = "Qwen/Qwen3-Embedding-0.6B"
DIM = 8


class FakeJobGroupStore:
    def __init__(self):
        self.groups: dict[str, JobGroup] = {}
        self.create_calls: list[dict] = []
        self.increment_calls = 0
        self.deleted: list[str] = []
        self._n = 0

    async def create(self, label, kind, created_by=None, task_count=0):
        self._n += 1
        gid = f"grp-{self._n}"
        self.groups[gid] = JobGroup(id=gid, label=label, kind=kind, created_by=created_by, task_count=task_count)
        self.create_calls.append({"label": label, "kind": kind, "created_by": created_by, "task_count": task_count})
        return gid

    async def latest_by_kind(self, kind):
        matching = [g for g in self.groups.values() if g.kind == kind]
        return max(matching, key=lambda g: g.created_at) if matching else None

    async def delete(self, group_id):
        self.deleted.append(group_id)
        self.groups.pop(group_id, None)

    async def increment_task_count(self, group_id, by=1):
        self.increment_calls += 1
        return 0


class FakeJobStore:
    def __init__(self, jobs: list[Job] | None = None):
        self.jobs: list[Job] = list(jobs or [])
        self.persisted: list[dict] = []

    async def list_by_status(self, status):
        return [j for j in self.jobs if j.status == status]

    async def list_by_group(self, group_id):
        return [j for j in self.jobs if j.group_id == group_id]


class FakeQueue:
    def __init__(self):
        self.enqueued: list[str] = []

    async def enqueue(self, job_id):
        self.enqueued.append(job_id)


def _source(id_, chunk_count=100, kind="repo", url="", path=""):
    return {
        "id": id_, "path": path, "url": url, "branch": "main", "indexed_at": 0,
        "file_count": 10, "chunk_count": chunk_count, "commit_sha": "", "graph_indexed": True,
        "created_by": "admin", "kind": kind,
    }


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", MODEL)
    monkeypatch.setenv("VECTOR_DIM", str(DIM))
    group_store = FakeJobGroupStore()
    job_store = FakeJobStore()
    queue = FakeQueue()
    monkeypatch.setattr(idx_state, "_job_group_store", group_store)
    monkeypatch.setattr(idx_state, "_job_store", job_store)
    monkeypatch.setattr(idx_state, "_job_queue", queue)
    monkeypatch.setattr(idx_state, "_community_build_state", {"status": "idle"})

    persisted = []

    async def fake_persist(jd):
        persisted.append(jd)
        job_store.jobs.append(Job.from_dict(jd))

    monkeypatch.setattr(runners, "_persist_job", fake_persist)

    calls = {"drop": 0, "init": 0, "vs_write": [], "gs_clear": 0, "gs_write": [], "invalidate": 0}

    async def _drop():
        calls["drop"] += 1

    async def _init():
        calls["init"] += 1

    async def _vs_write(stamp):
        calls["vs_write"].append(stamp)

    async def _gs_clear():
        calls["gs_clear"] += 1

    async def _gs_write(stamp):
        calls["gs_write"].append(stamp)

    def _invalidate():
        calls["invalidate"] += 1

    matching_stamp = IndexStamp(schema=1, embedding_model=MODEL, vector_dim=DIM)

    async def _vs_observe():
        # After a rebuild, init_collection() has stamped it (the adapters'
        # own behaviour, exercised in T009/T013 — not re-tested here).
        return StoreObservation(
            store="vector", backend="fake", exists=True, has_data=False,
            stamp=matching_stamp, schema_dim=DIM,
        )

    async def _gs_observe():
        return StoreObservation(
            store="graph", backend="fake", exists=True, has_data=False, stamp=matching_stamp,
        )

    monkeypatch.setattr(retriever, "drop_index", _drop)
    monkeypatch.setattr(retriever, "init_collection", _init)
    monkeypatch.setattr(retriever, "write_stamp", _vs_write)
    monkeypatch.setattr(retriever, "observe_index", _vs_observe)
    monkeypatch.setattr(graph_store, "clear_index_data", _gs_clear)
    monkeypatch.setattr(graph_store, "write_stamp", _gs_write)
    monkeypatch.setattr(graph_store, "observe_index", _gs_observe)
    monkeypatch.setattr(retrieval, "invalidate_graph_caches", _invalidate)

    async def _probe_none():
        return None

    monkeypatch.setattr(maintenance_lock, "probe", _probe_none)

    yield {"group_store": group_store, "job_store": job_store, "queue": queue, "persisted": persisted, "calls": calls}


async def _set_sources(monkeypatch, sources: list[dict]):
    async def _all():
        return sources

    monkeypatch.setattr(idx_svc, "_all_source_dicts", _all)


class TestDryRun:
    async def test_lists_sources_and_totals_and_writes_nothing(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1", 100), _source("s2", 50)])

        result = await ig.rebuild(dry_run=True)

        assert result["dry_run"] is True
        assert {s["id"] for s in result["sources"]} == {"s1", "s2"}
        assert result["total_sources"] == 2
        assert result["total_chunks"] == 150
        assert result["blockers"] == []
        assert _base["calls"]["drop"] == 0
        assert _base["group_store"].create_calls == []
        assert _base["queue"].enqueued == []

    async def test_lists_current_blockers(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [])
        _base["job_store"].jobs.append(Job(id="j1", status=JobStatus.RUNNING))

        result = await ig.rebuild(dry_run=True)

        assert result["blockers"]
        assert any("running" in b for b in result["blockers"])


class TestRealRebuildOrder:
    async def test_full_order_and_stamps(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1", 100)])

        acquired = []

        class FakeHandle:
            def __init__(self):
                self.released = False

            async def release(self):
                self.released = True

        async def _acquire(mode):
            acquired.append(mode)
            return FakeHandle()

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)

        result = await ig.rebuild(dry_run=False, user_id="admin-1")

        assert result["dry_run"] is False
        assert acquired == ["exclusive"]
        group_id = result["group_id"]
        assert _base["group_store"].create_calls == [
            {"label": "index rebuild", "kind": "index-rebuild", "created_by": "admin-1", "task_count": 1}
        ]
        assert _base["calls"]["drop"] == 1
        # init_collection() is what stamps the vector store (it always
        # stamps a collection it creates — see adapters/*/vector_store.py);
        # rebuild() never calls retriever.write_stamp directly.
        assert _base["calls"]["init"] == 1
        assert _base["calls"]["vs_write"] == []
        assert _base["calls"]["gs_clear"] == 1
        assert _base["calls"]["gs_write"] == [IndexStamp(1, MODEL, DIM)]
        assert _base["calls"]["invalidate"] >= 1
        assert len(_base["queue"].enqueued) == 1
        assert _base["persisted"][0]["group_id"] == group_id
        assert _base["group_store"].increment_calls == 0  # preset, never incremented

    async def test_summary_cache_untouched(self, monkeypatch, _base):
        """No summary_cache access anywhere in the rebuild path — nothing to
        fake means nothing was called."""
        await _set_sources(monkeypatch, [_source("s1", 10)])

        async def _acquire(mode):
            class H:
                async def release(self):
                    pass
            return H()

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        await ig.rebuild(dry_run=False)
        # No assertion needed beyond "it ran" — summary_cache has no fake
        # installed, so any access would raise ImportError/AttributeError.


class TestBlockersRefuseBeforeAnythingDrops:
    async def test_running_job_refuses_with_409_and_drops_nothing(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1")])
        _base["job_store"].jobs.append(Job(id="j1", status=JobStatus.RUNNING))

        with pytest.raises(ig.RebuildRefused) as exc_info:
            await ig.rebuild(dry_run=False)

        assert "running" in exc_info.value.detail
        assert _base["calls"]["drop"] == 0
        assert _base["group_store"].create_calls == []

    async def test_community_build_holding_shared_lock_refuses(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1")])

        async def _probe_shared():
            return "shared"

        monkeypatch.setattr(maintenance_lock, "probe", _probe_shared)

        with pytest.raises(ig.RebuildRefused):
            await ig.rebuild(dry_run=False)
        assert _base["calls"]["drop"] == 0

    async def test_rebuild_already_running_refuses(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1")])

        async def _probe_exclusive():
            return "exclusive"

        monkeypatch.setattr(maintenance_lock, "probe", _probe_exclusive)

        with pytest.raises(ig.RebuildRefused):
            await ig.rebuild(dry_run=False)
        assert _base["calls"]["drop"] == 0

    async def test_acquire_returning_none_refuses(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1")])

        async def _acquire_none(mode):
            return None

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire_none)

        with pytest.raises(ig.RebuildRefused) as exc_info:
            await ig.rebuild(dry_run=False)
        assert "in progress" in exc_info.value.detail
        assert _base["calls"]["drop"] == 0

    async def test_no_postgres_pool_raises_runtime_error(self, monkeypatch, _base):
        monkeypatch.setattr(idx_state, "_job_group_store", None)
        with pytest.raises(RuntimeError):
            await ig.rebuild(dry_run=False)


class TestStep3RecheckAbortsCleanly:
    async def test_job_that_became_running_after_step0_aborts_and_deletes_group(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1")])

        class FakeHandle:
            def __init__(self):
                self.released = False

            async def release(self):
                self.released = True

        handle = FakeHandle()

        async def _acquire(mode):
            # Simulate a job becoming "running" exactly as the lock is taken.
            _base["job_store"].jobs.append(Job(id="late-job", status=JobStatus.RUNNING))
            return handle

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)

        with pytest.raises(ig.RebuildRefused):
            await ig.rebuild(dry_run=False)

        assert _base["calls"]["drop"] == 0
        assert len(_base["group_store"].deleted) == 1
        assert handle.released is True


class TestInterruptedRebuildIsNotABlocker:
    async def test_dry_run_after_interrupted_rebuild_lists_no_blocker(self, monkeypatch, _base):
        gid = await _base["group_store"].create("index rebuild", "index-rebuild", task_count=5)
        # Only 1 of 5 jobs exists, none active: interrupted.
        _base["job_store"].jobs.append(Job(id="j1", status=JobStatus.DONE, group_id=gid))
        await _set_sources(monkeypatch, [_source("s1")])

        result = await ig.rebuild(dry_run=True)
        assert result["blockers"] == []

    async def test_real_rebuild_after_interrupted_succeeds(self, monkeypatch, _base):
        gid = await _base["group_store"].create("index rebuild", "index-rebuild", task_count=5)
        _base["job_store"].jobs.append(Job(id="j1", status=JobStatus.DONE, group_id=gid))
        await _set_sources(monkeypatch, [_source("s1")])

        async def _acquire(mode):
            class H:
                async def release(self):
                    pass
            return H()

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        result = await ig.rebuild(dry_run=False)
        assert result["dry_run"] is False


class TestStoreFailure:
    async def test_drop_index_raising_reports_failed_and_reindex_required(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1")])

        async def _acquire(mode):
            class H:
                async def release(self):
                    pass
            return H()

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)

        async def _boom():
            raise RuntimeError("milvus unreachable")

        monkeypatch.setattr(retriever, "drop_index", _boom)

        with pytest.raises(ig.RebuildFailed) as exc_info:
            await ig.rebuild(dry_run=False)

        assert exc_info.value.step == "recreate stores"
        assert ig.status().state == "reindex_required"
        assert "recreate stores" in ig.status().reason


class TestProgressAndCompletion:
    async def test_health_shows_rebuilding_with_progress(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1"), _source("s2"), _source("s3")])

        async def _acquire(mode):
            class H:
                async def release(self):
                    pass
            return H()

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        await ig.rebuild(dry_run=False)

        # One job done, two still queued.
        for j in _base["job_store"].jobs[:1]:
            j.status = JobStatus.DONE
        status = await ig.refresh()
        assert status.state == "rebuilding"
        assert status.rebuild_progress == (1, 3)

    async def test_search_allowed_while_rebuilding(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1")])

        async def _acquire(mode):
            class H:
                async def release(self):
                    pass
            return H()

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        await ig.rebuild(dry_run=False)
        assert await ig.refresh()
        assert ig.require_searchable() is None

    async def test_all_jobs_terminal_including_one_failed_becomes_ok(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1"), _source("s2")])

        async def _acquire(mode):
            class H:
                async def release(self):
                    pass
            return H()

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        await ig.rebuild(dry_run=False)

        _base["job_store"].jobs[0].status = JobStatus.DONE
        _base["job_store"].jobs[1].status = JobStatus.FAILED
        status = await ig.refresh()
        assert status.state == "ok"

    async def test_crash_after_drop_before_all_jobs_enqueued_is_interrupted(self, monkeypatch, _base):
        """A group with fewer jobs than task_count and none active reports
        `reindex_required` ("a rebuild was interrupted")."""
        gid = await _base["group_store"].create("index rebuild", "index-rebuild", task_count=3)
        # Only 1 of 3 jobs made it in before the simulated crash.
        _base["job_store"].jobs.append(Job(id="j1", status=JobStatus.DONE, group_id=gid))

        status = await ig.run_check()
        assert status.state == "reindex_required"
        assert "interrupted" in status.reason

    async def test_zero_sources_completes_immediately(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [])

        async def _acquire(mode):
            class H:
                async def release(self):
                    pass
            return H()

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        result = await ig.rebuild(dry_run=False)
        assert result["total_sources"] == 0
        status = await ig.refresh()
        assert status.state == "ok"


class TestCommunityBuildTakesTheSharedLock:
    """T038: POST /build-community holds the maintenance lock shared for
    the whole backfill task, releases it when the task raises, and a
    denied acquire returns 409."""

    async def test_lock_held_for_the_tasks_lifetime_and_released_after(self, monkeypatch, _base):
        from fastapi.testclient import TestClient

        from treeweft.application.indexer_service import app

        released_at_call = []

        class FakeHandle:
            async def release(self):
                released_at_call.append("released")

        acquire_calls = []

        async def _acquire(mode):
            acquire_calls.append(mode)
            return FakeHandle()

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        monkeypatch.setattr(graph_store, "list_sources", AsyncMockCoro([]))
        monkeypatch.setattr(idx_svc.authz, "_require_admin", lambda request: None)

        client = TestClient(app)
        resp = client.post("/build-community")
        assert resp.status_code == 202
        assert acquire_calls == ["shared"]

        task = idx_state._community_build_task
        assert task is not None
        await task
        assert released_at_call == ["released"]

    async def test_lock_released_when_the_backfill_task_raises(self, monkeypatch, _base):
        from fastapi.testclient import TestClient

        from treeweft.application.indexer_service import app

        released_at_call = []

        class FakeHandle:
            async def release(self):
                released_at_call.append("released")

        async def _acquire(mode):
            return FakeHandle()

        async def _boom():
            raise RuntimeError("graph store exploded")

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        monkeypatch.setattr(graph_store, "list_sources", _boom)
        monkeypatch.setattr(idx_svc.authz, "_require_admin", lambda request: None)

        client = TestClient(app)
        resp = client.post("/build-community")
        assert resp.status_code == 202

        task = idx_state._community_build_task
        await task
        assert released_at_call == ["released"]
        assert idx_state._community_build_state["status"] == "failed"

    async def test_denied_acquire_returns_409(self, monkeypatch, _base):
        from fastapi.testclient import TestClient

        from treeweft.application.indexer_service import app

        async def _acquire(mode):
            return None  # a rebuild is preparing

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        monkeypatch.setattr(idx_svc.authz, "_require_admin", lambda request: None)

        client = TestClient(app)
        resp = client.post("/build-community")
        assert resp.status_code == 409


class AsyncMockCoro:
    """A callable returning `value` when awaited, for a bare async function
    replacement (`graph_store.list_sources` takes no args)."""

    def __init__(self, value):
        self._value = value

    async def __call__(self):
        return self._value


class TestRebuildRoute:
    """T042: POST /index/rebuild — status codes and admin gating."""

    def _client(self, monkeypatch, admin=True):
        from fastapi.testclient import TestClient

        from treeweft.application.indexer_service import app

        if admin:
            monkeypatch.setattr(idx_svc.authz, "_require_admin", lambda request: None)
        return TestClient(app, raise_server_exceptions=False)

    async def test_dry_run_is_200(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1")])
        client = self._client(monkeypatch)
        resp = client.post("/index/rebuild", params={"dry_run": "true"})
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is True

    async def test_real_call_is_202(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1")])

        async def _acquire(mode):
            class H:
                async def release(self):
                    pass
            return H()

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        client = self._client(monkeypatch)
        resp = client.post("/index/rebuild")
        assert resp.status_code == 202
        assert resp.json()["dry_run"] is False

    async def test_blocked_call_is_409(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1")])
        _base["job_store"].jobs.append(Job(id="j1", status=JobStatus.RUNNING))
        client = self._client(monkeypatch)
        resp = client.post("/index/rebuild")
        assert resp.status_code == 409
        assert "blockers" in resp.json()

    async def test_store_failure_is_503(self, monkeypatch, _base):
        await _set_sources(monkeypatch, [_source("s1")])

        async def _acquire(mode):
            class H:
                async def release(self):
                    pass
            return H()

        async def _boom():
            raise RuntimeError("milvus unreachable")

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        monkeypatch.setattr(retriever, "drop_index", _boom)
        client = self._client(monkeypatch)
        resp = client.post("/index/rebuild")
        assert resp.status_code == 503

    async def test_no_postgres_is_503(self, monkeypatch, _base):
        monkeypatch.setattr(idx_state, "_job_group_store", None)
        client = self._client(monkeypatch)
        resp = client.post("/index/rebuild")
        assert resp.status_code == 503

    async def test_non_admin_is_403_or_401(self, monkeypatch, _base):
        client = self._client(monkeypatch, admin=False)
        resp = client.post("/index/rebuild")
        assert resp.status_code in (401, 403)


class TestLogging:
    async def test_dry_run_and_real_call_both_log_a_warning(self, monkeypatch, _base, caplog):
        await _set_sources(monkeypatch, [_source("s1", 42)])

        with caplog.at_level("WARNING", logger="treeweft.application.index_guard"):
            await ig.rebuild(dry_run=True)
        assert any("event=index_rebuild" in r.message and "dry_run=True" in r.message for r in caplog.records)

        caplog.clear()

        async def _acquire(mode):
            class H:
                async def release(self):
                    pass
            return H()

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        with caplog.at_level("WARNING", logger="treeweft.application.index_guard"):
            await ig.rebuild(dry_run=False)
        assert any("event=index_rebuild" in r.message and "dry_run=False" in r.message for r in caplog.records)
