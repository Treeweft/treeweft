"""Index request options reach the job that runs (#51).

Index jobs run from the Postgres queue: a worker rebuilds each job from its
stored row (`dispatch_job`), and only stored fields survive. `pattern`,
`skip_patterns` and `skip_graph` were set on the in-memory job, or not at
all, so a queued job never saw them: `index_directory(pattern=...)` indexed
every file, and `skip_graph=true` (two-pass indexing) still built the graph.
They are now kept in the job's payload, which is stored.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tests.unit.test_refresh_preemption import (  # noqa: F401  (fixtures)
    InMemoryJobStore,
    InMemoryQueue,
    _fake_request,
    _no_job_group_store,
    _writable_index,
)
from treeweft.application import indexer_runners as runners
from treeweft.application import indexer_service as idx_svc
from treeweft.application import indexer_state as idx_state
from treeweft.domain.jobs import Job, JobStatus

pytestmark = pytest.mark.asyncio


@pytest.fixture
def queued(monkeypatch):
    """Handlers write to an in-memory store and queue; returns the store."""
    store = InMemoryJobStore([])
    monkeypatch.setattr(idx_state, "_job_store", store)
    monkeypatch.setattr(idx_state, "_job_queue", InMemoryQueue())
    monkeypatch.setattr(idx_svc, "FEATURE_SKIP_PATTERNS", True)
    return store


async def _stored(store, job_id) -> Job:
    job = await store.get(job_id)
    assert job is not None
    return job


class _Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)

        async def _noop():
            return None

        return _noop()


# ── the handlers store the options ───────────────────────────────────────

async def test_directory_options_are_stored_with_the_job(queued, tmp_path):
    req = idx_svc.IndexDirectoryRequest(
        directory=str(tmp_path), pattern="**/*.py", auto_preflight=False,
        skip_patterns=["*_test.py"], skip_graph=True,
    )
    ack = await idx_svc.handle_index_directory(req, _fake_request())

    payload = (await _stored(queued, ack.job_id)).payload or {}
    assert payload["pattern"] == "**/*.py"
    assert payload["skip_patterns"] == ["*_test.py"]
    assert payload["skip_graph"] is True


async def test_repo_options_are_stored_with_the_job(queued, tmp_path):
    req = idx_svc.IndexRepoRequest(
        path=str(tmp_path), auto_preflight=False, skip_patterns=["vendor/*"], skip_graph=True,
    )
    ack = await idx_svc.handle_index_repo(req, _fake_request())

    payload = (await _stored(queued, ack.job_id)).payload or {}
    assert payload["skip_patterns"] == ["vendor/*"]
    assert payload["skip_graph"] is True


async def test_file_skip_graph_is_stored_with_the_job(queued, tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    ack = await idx_svc.handle_index_file(
        idx_svc.IndexFileRequest(file_path=str(f), skip_graph=True), _fake_request(),
    )

    assert ((await _stored(queued, ack.job_id)).payload or {})["skip_graph"] is True


async def test_skip_patterns_are_not_stored_while_the_feature_is_off(queued, tmp_path, monkeypatch):
    monkeypatch.setattr(idx_svc, "FEATURE_SKIP_PATTERNS", False)
    req = idx_svc.IndexDirectoryRequest(
        directory=str(tmp_path), auto_preflight=False, skip_patterns=["*_test.py"],
    )
    ack = await idx_svc.handle_index_directory(req, _fake_request())

    assert "skip_patterns" not in ((await _stored(queued, ack.job_id)).payload or {})


# ── the queue worker rebuilds the job with them ──────────────────────────

def _row(kind, source_path, payload) -> Job:
    return Job(
        id=f"j-{kind}", kind=kind, source_id=f"s-{kind}", status=JobStatus.QUEUED,
        source_path=source_path, payload=payload,
    )


async def test_dispatched_directory_job_gets_its_pattern_and_options(tmp_path, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(runners, "_run_index_directory_job", rec)
    job = _row("directory", str(tmp_path), {
        "pattern": "**/*.py", "skip_patterns": ["*_test.py"], "skip_graph": True,
    })

    coro = runners.dispatch_job(job)
    await coro

    jd, directory, pattern = rec.calls[0]
    assert (directory, pattern) == (str(tmp_path), "**/*.py")
    assert jd["skip_patterns"] == ["*_test.py"]
    assert jd["skip_graph"] is True


async def test_dispatched_directory_job_without_options_indexes_everything(tmp_path, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(runners, "_run_index_directory_job", rec)

    await runners.dispatch_job(_row("directory", str(tmp_path), None))

    jd, _directory, pattern = rec.calls[0]
    assert pattern == "**/*"
    assert not jd.get("skip_graph")
    assert not jd.get("skip_patterns")


async def test_dispatched_repo_job_gets_skip_graph_and_skip_patterns(tmp_path, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(runners, "_run_index_repo_job", rec)
    job = _row("repo", str(tmp_path), {"skip_patterns": ["vendor/*"], "skip_graph": True})

    await runners.dispatch_job(job)

    jd, spec = rec.calls[0]
    assert spec.skip_graph is True
    assert jd["skip_graph"] is True
    assert jd["skip_patterns"] == ["vendor/*"]


async def test_dispatched_file_job_gets_skip_graph(tmp_path, monkeypatch):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    rec = _Recorder()
    monkeypatch.setattr(runners, "_run_index_file_job", rec)
    job = _row("file", str(f), {"skip_graph": True})

    await runners.dispatch_job(job)

    jd, _target = rec.calls[0]
    assert jd["skip_graph"] is True


# ── the file job honours skip_graph ──────────────────────────────────────

async def test_file_job_with_skip_graph_builds_no_graph(tmp_path, monkeypatch):
    f = tmp_path / "a.py"
    f.write_text("class A:\n    def f(self):\n        return 1\n")
    stored = []

    async def store_graph(entities, relationships, source_id=None):
        stored.extend(entities)

    async def embed(texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr(runners.graph_store, "store_graph", store_graph)
    monkeypatch.setattr(runners.graph_store, "upsert_source", AsyncMock())
    monkeypatch.setattr(runners, "embed", embed)
    monkeypatch.setattr(runners, "insert_chunks", AsyncMock())
    monkeypatch.setattr(runners, "init_collection", AsyncMock())
    monkeypatch.setattr(runners, "_reset_source", AsyncMock())
    monkeypatch.setattr(runners, "_persist_job", AsyncMock())
    monkeypatch.setattr(runners, "USE_SUMMARY_VECTOR", False)

    class _Repo:
        saved: list = []

        async def save(self, record):
            self.saved.append(record)

        async def get_by_id(self, source_id):
            return None

        async def record_summary_version(self, *a):
            return None

    repo = _Repo()
    monkeypatch.setattr(idx_state, "_source_repo", repo)
    job = runners._new_job("file", "s-file", str(f))
    job["skip_graph"] = True

    await runners._run_index_file_job(job, str(f))

    assert job["status"] == "done", job.get("error")
    assert stored == []
    assert repo.saved[-1].graph_indexed is False
