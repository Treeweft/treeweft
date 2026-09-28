"""The graph-only pass of two-pass indexing (`/index-graph`) (#51).

With skip_graph now honoured and graph_indexed now stored, the second pass
is reachable for every kind of source, which exposed four gaps:

- a file source could not be graph-indexed: the job required a directory;
- the pass graphed every supported file under the path, ignoring the
  pattern (or skip patterns) that chose the chunk-indexed files;
- finishing it rewrote the whole source record from the graph job's own
  counters: chunk_count 0, and graph_indexed true even when no file
  succeeded, overriding the job's own rule;
- a resumed full job recorded only the files it indexed after resuming.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from treeweft.application import indexer_runners as svc
from treeweft.application import indexer_state as _state
from treeweft.domain.sources import SourceRecord

pytestmark = pytest.mark.asyncio

PY = "class A:\n    def f(self):\n        return 1\n"


class _Repo:
    def __init__(self, record: SourceRecord | None):
        self.record = record
        self.saved: list[SourceRecord] = []
        self.flags: list[bool] = []

    async def get_by_id(self, source_id):
        return self.record

    async def save(self, record):
        self.saved.append(record)
        self.record = record

    async def set_graph_indexed(self, source_id, indexed, when=None):
        self.flags.append(indexed)
        return True

    async def record_summary_version(self, *a):
        return None


@pytest.fixture
def graphed(monkeypatch):
    """Files passed to the graph step, in order."""
    seen: list[str] = []

    async def graph_index_file(file_path, source_id):
        seen.append(file_path)

    monkeypatch.setattr(svc, "_graph_index_file", graph_index_file)
    monkeypatch.setattr(svc.graph_store, "delete_source", AsyncMock())
    monkeypatch.setattr(svc.graph_store, "upsert_source", AsyncMock())
    monkeypatch.setattr(svc, "_persist_job", AsyncMock())
    monkeypatch.setattr(_state, "_job_file_error_store", AsyncMock())
    return seen


def _indexed_paths(monkeypatch, paths):
    async def list_indexed_paths(source_id):
        return set(paths)

    monkeypatch.setattr("treeweft.retriever.list_indexed_paths", list_indexed_paths)


def _record(path, **overrides) -> SourceRecord:
    fields = dict(
        id="src", path=str(path), url="", branch="", kind="directory",
        indexed_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
        file_count=3, chunk_count=40, graph_indexed=False,
    )
    fields.update(overrides)
    return SourceRecord(**fields)


async def _graph_job(repo):
    job = svc._new_job("graph", "src", repo.record.path)
    await svc._run_index_graph_job(job, "src")
    return job


async def test_a_file_source_can_be_graph_indexed(tmp_path, graphed, monkeypatch):
    f = tmp_path / "a.py"
    f.write_text(PY)
    _indexed_paths(monkeypatch, [str(f)])
    repo = _Repo(_record(f, kind="file", file_count=1))
    monkeypatch.setattr(_state, "_source_repo", repo)

    job = await _graph_job(repo)

    assert job["status"] == "done", job.get("error")
    assert graphed == [str(f)]
    assert repo.flags == [True]


async def test_only_files_with_chunks_are_graphed(tmp_path, graphed, monkeypatch):
    """The chunk pass chose the files (pattern, skip patterns); the graph
    pass follows it rather than re-walking the whole tree."""
    for name in ("a.py", "b.py", "notes.md", "c.go"):
        (tmp_path / name).write_text(PY if name.endswith(".py") else "package c\n")
    _indexed_paths(monkeypatch, [str(tmp_path / "a.py"), str(tmp_path / "b.py")])
    repo = _Repo(_record(tmp_path))
    monkeypatch.setattr(_state, "_source_repo", repo)

    job = await _graph_job(repo)

    assert sorted(graphed) == [str(tmp_path / "a.py"), str(tmp_path / "b.py")]
    assert job["total_files"] == 2


async def test_a_store_without_path_tracking_graphs_every_file(tmp_path, graphed, monkeypatch):
    """ChromaDB reports no indexed paths; fall back to the whole tree."""
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text(PY)
    _indexed_paths(monkeypatch, [])
    repo = _Repo(_record(tmp_path))
    monkeypatch.setattr(_state, "_source_repo", repo)

    await _graph_job(repo)

    assert len(graphed) == 2


async def test_finishing_keeps_the_source_record(tmp_path, graphed, monkeypatch):
    (tmp_path / "a.py").write_text(PY)
    _indexed_paths(monkeypatch, [str(tmp_path / "a.py")])
    original = _record(tmp_path, file_count=3, chunk_count=40)
    repo = _Repo(original)
    monkeypatch.setattr(_state, "_source_repo", repo)

    await _graph_job(repo)

    assert repo.saved == []  # graph_indexed is set by set_graph_indexed alone
    assert repo.record.chunk_count == 40
    assert repo.flags == [True]


async def test_no_success_leaves_the_source_not_graph_indexed(tmp_path, graphed, monkeypatch):
    (tmp_path / "a.py").write_text(PY)
    _indexed_paths(monkeypatch, [str(tmp_path / "a.py")])

    async def failing(file_path, source_id):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(svc, "_graph_index_file", failing)
    repo = _Repo(_record(tmp_path))
    monkeypatch.setattr(_state, "_source_repo", repo)

    await _graph_job(repo)

    assert repo.flags == []
    assert repo.record.graph_indexed is False


async def test_resumed_job_records_every_file_of_the_source(tmp_path, monkeypatch):
    """_walk_and_index skips the files already in the store on resume; the
    count it returns is still the whole source."""
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text(PY)
    _indexed_paths(monkeypatch, [str(tmp_path / "a.py"), str(tmp_path / "b.py")])

    async def process_file(file_path, source_id, **kwargs):
        return 2

    monkeypatch.setattr(svc, "_process_file", process_file)
    monkeypatch.setattr(svc, "_persist_job", AsyncMock())
    job = svc._new_job("directory", "src", str(tmp_path))

    _chunks, total_files = await svc._walk_and_index(str(tmp_path), "src", job, skip_count=2)

    assert total_files == 3
    assert job["total_files"] == 3


async def test_a_finished_graph_job_is_still_persisted_as_done(tmp_path, graphed, monkeypatch):
    (tmp_path / "a.py").write_text(PY)
    _indexed_paths(monkeypatch, [str(tmp_path / "a.py")])
    repo = _Repo(_record(tmp_path))
    monkeypatch.setattr(_state, "_source_repo", repo)
    persisted = []

    async def persist(job):
        persisted.append(dict(job))

    monkeypatch.setattr(svc, "_persist_job", persist)

    await _graph_job(repo)

    assert persisted and persisted[-1]["status"] == "done"
