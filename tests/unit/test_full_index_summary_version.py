"""Full-index jobs and the recorded chunk-summary version (T013, research R6,
research R7, FR-010).

A fake source repo stands in for Postgres, `summary_vectors_supported` is
monkeypatched to stand in for the vector store, and summaries are faked at
`_summaries_for_chunks`/`llm.cache_get_many`/`llm.summarize_with_cache` so no
real LLM or vector store is touched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from treeweft.application import indexer_runners as svc
from treeweft.application import indexer_state as _state
from treeweft.application import prompt_pins



class _FakeSourceRepo:
    def __init__(self):
        self.recorded: list[tuple[str, int | None]] = []
        self.saved: list = []

    async def record_summary_version(self, source_id, version):
        self.recorded.append((source_id, version))

    async def save(self, record):
        self.saved.append(record)

    async def get_by_id(self, source_id):
        return None

    async def set_graph_indexed(self, *args, **kwargs):
        return None


@pytest.fixture
def fake_repo(monkeypatch):
    repo = _FakeSourceRepo()
    monkeypatch.setattr(_state, "_source_repo", repo)
    return repo


@pytest.fixture(autouse=True)
def _no_real_io(monkeypatch):
    monkeypatch.setattr(svc, "_persist_job", AsyncMock())
    monkeypatch.setattr(svc, "_persist_job_sync", lambda job: None)
    monkeypatch.setattr(svc.graph_store, "upsert_source", AsyncMock())
    monkeypatch.setattr(svc, "_reset_source", AsyncMock())


def _pin(monkeypatch, version: int):
    monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: version)


def _base_job(kind: str, source_id: str, version: int | None, **extra) -> dict:
    job = {
        "job_id": f"j-{source_id}",
        "source_id": source_id,
        "kind": kind,
        "errors": 0,
        "payload": {"summary_version": version} if version is not None else {},
        "source_path": "/x",
        "source_url": "",
        "source_branch": "",
    }
    job.update(extra)
    return job


# ---------------------------------------------------------------------------
# _finalize_job: research R6's rule, by kind
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clean_repo_job_records_the_payload_version(fake_repo, monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)
    job = _base_job("repo", "s-repo", 5)
    await svc._finalize_job(job, 3, 1, "ok")
    assert job["status"] == "done"
    assert fake_repo.recorded == [("s-repo", 5)]


@pytest.mark.parametrize("kind", ["file", "directory"])
@pytest.mark.asyncio
async def test_non_summarizing_full_job_records_unknown(kind, fake_repo, monkeypatch):
    # File and directory jobs insert no summary vectors, so a v3 record would
    # report empty summary vectors as current.
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)
    job = _base_job(kind, f"s-{kind}", 5)
    await svc._finalize_job(job, 3, 1, "ok")
    assert job["status"] == "done"
    assert fake_repo.recorded == [(f"s-{kind}", None)]


@pytest.mark.asyncio
async def test_file_error_does_not_record(fake_repo, monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)
    job = _base_job("repo", "s-fileerr", 5, errors=1)
    await svc._finalize_job(job, 3, 2, "partial")
    assert job["status"] == "done"
    assert fake_repo.recorded == []


@pytest.mark.asyncio
async def test_transient_summary_error_does_not_record(fake_repo, monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)
    job = _base_job("repo", "s-sumerr", 5, summary_errors=1)
    await svc._finalize_job(job, 3, 1, "ok")
    assert job["status"] == "done"
    assert job["summary_errors"] == 1
    assert fake_repo.recorded == []


@pytest.mark.asyncio
async def test_summary_vectors_off_records_none(fake_repo, monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", False)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)
    job = _base_job("directory", "s-off", 5)
    await svc._finalize_job(job, 3, 1, "ok")
    assert fake_repo.recorded == [("s-off", None)]


@pytest.mark.asyncio
async def test_summary_vectors_unsupported_records_none(fake_repo, monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: False)
    job = _base_job("file", "s-unsupported", 5)
    await svc._finalize_job(job, 3, 1, "ok")
    assert fake_repo.recorded == [("s-unsupported", None)]


@pytest.mark.asyncio
async def test_summary_vectors_off_still_records_none_despite_file_errors(fake_repo, monkeypatch):
    # R6: the off/unsupported branch is unconditional -- NULL just means
    # "never summarized", which stays true regardless of unrelated file
    # errors.
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", False)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)
    job = _base_job("repo", "s-off-err", 5, errors=2)
    await svc._finalize_job(job, 3, 3, "partial")
    assert fake_repo.recorded == [("s-off-err", None)]


@pytest.mark.asyncio
async def test_failed_job_never_records(fake_repo, monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)
    job = _base_job("repo", "s-failed", 5, errors=3)
    await svc._finalize_job(job, 0, 3, "all failed")
    assert job["status"] == "failed"
    assert fake_repo.recorded == []


@pytest.mark.asyncio
async def test_graph_job_never_records(fake_repo, monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)
    job = _base_job("graph", "s-graph", 5)
    await svc._finalize_job(job, 0, 1, "graph done")
    assert fake_repo.recorded == []


@pytest.mark.asyncio
async def test_incremental_job_never_records(fake_repo):
    job = {"job_id": "j-incr", "source_id": "s-incr", "errors": 0, "files_removed": 0}
    await svc._finalize_incremental_job(job, 3, 1, "ok")
    assert fake_repo.recorded == []


# ---------------------------------------------------------------------------
# treeweft.retriever.summary_vectors_supported exists for every backend
# ---------------------------------------------------------------------------

def test_summary_vectors_supported_by_backend():
    from treeweft.adapters.chromadb import vector_store as chroma
    from treeweft.adapters.lancedb import vector_store as lancedb
    from treeweft.adapters.milvus import vector_store as milvus

    assert milvus.summary_vectors_supported() is True
    assert lancedb.summary_vectors_supported() is True
    assert chroma.summary_vectors_supported() is False


# ---------------------------------------------------------------------------
# _resolve_summary_version: resolved once, persisted once, reused on resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_summary_version_sets_payload_and_persists(monkeypatch):
    _pin(monkeypatch, 9)
    persisted = []

    async def _persist(job):
        persisted.append(dict(job))

    monkeypatch.setattr(svc, "_persist_job", _persist)
    job = {"job_id": "j-resolve", "source_id": "s-resolve"}
    version = await svc._resolve_summary_version(job)
    assert version == 9
    assert job["payload"]["summary_version"] == 9
    assert persisted and persisted[-1]["payload"]["summary_version"] == 9


@pytest.mark.asyncio
async def test_resolve_summary_version_reuses_stored_value_after_pin_moves(monkeypatch):
    _pin(monkeypatch, 9)
    calls = []

    async def _persist(job):
        calls.append(1)

    monkeypatch.setattr(svc, "_persist_job", _persist)
    job = {"job_id": "j-resume", "source_id": "s-resume", "payload": {"summary_version": 3}}
    version = await svc._resolve_summary_version(job)
    assert version == 3  # the stored value, not the moved pin
    assert calls == []  # nothing changed -- no re-persist


# ---------------------------------------------------------------------------
# _summaries_for_chunks: cache hits, rejection markers, generated, error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summaries_for_chunks_maps_every_strategy(monkeypatch):
    chunks = [
        {"text": "def a(): pass", "file_path": "a.py", "language": "python"},
        {"text": "def b(): pass", "file_path": "b.py", "language": "python"},
        {"text": "def c(): pass", "file_path": "c.py", "language": "python"},
    ]
    ka, kb, kc = (svc.llm.chunk_cache_key(c["text"]) for c in chunks)

    async def _get_many(sha1s, prompt_version, include_rejected=False):
        rows = {ka: "Defines a.", kb: ""}
        return {k: v for k, v in rows.items() if v or include_rejected}

    async def _summarize(text, language, file_path, *, version):
        assert file_path == "c.py"
        assert version == 3
        return None, "error"

    monkeypatch.setattr(svc.llm, "cache_get_many", _get_many)
    monkeypatch.setattr(svc.llm, "summarize_with_cache", _summarize)

    outcomes = await svc._summaries_for_chunks(chunks, version=3)
    assert outcomes == [("Defines a.", "cached"), (None, "rejected"), (None, "error")]


# ---------------------------------------------------------------------------
# _process_file: counts "error" outcomes into job["summary_errors"], mirrored
# into job["payload"]["summary_errors"]
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_file_counts_error_outcomes_into_job_summary_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(
        svc.indexer, "parse_and_chunk",
        lambda fp: [{"text": "def a(): pass", "file_path": fp, "language": "python"}],
    )

    async def _embed(texts):
        return [[0.0] for _ in texts]

    monkeypatch.setattr(svc, "embed", _embed)
    monkeypatch.setattr(svc, "init_collection", AsyncMock())
    monkeypatch.setattr(svc, "insert_chunks", AsyncMock())
    monkeypatch.setattr(svc, "_graph_index_file", AsyncMock())

    async def _fake_summaries(chunks, *, version):
        assert version == 11
        return [(None, "error")]

    monkeypatch.setattr(svc, "_summaries_for_chunks", _fake_summaries)

    job = {"payload": {"summary_version": 11}}
    n = await svc._process_file(str(tmp_path / "a.py"), "src", version=11, job=job)
    assert n == 1
    assert job["summary_errors"] == 1
    assert job["payload"]["summary_errors"] == 1


# ---------------------------------------------------------------------------
# End-to-end wiring: a runner resolves the version at start and finalize
# records it (empty directory / repo path so no real file I/O is needed).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_directory_job_resolves_version_and_finalizes_clean(tmp_path, fake_repo, monkeypatch):
    _pin(monkeypatch, 4)
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)
    job = svc._new_job("directory", "srcdir", str(tmp_path))
    await svc._run_index_directory_job(job, str(tmp_path), "**/*")
    assert job["status"] == "done"
    assert job["payload"]["summary_version"] == 4
    assert fake_repo.recorded == [("srcdir", None)]


@pytest.mark.asyncio
async def test_repo_job_resolves_version_and_finalizes_clean(tmp_path, fake_repo, monkeypatch):
    _pin(monkeypatch, 6)
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)
    job = svc._new_job("repo", "srcrepo", str(tmp_path))
    req = svc._RepoJobSpec(path=str(tmp_path))
    await svc._run_index_repo_job(job, req)
    assert job["status"] == "done"
    assert job["payload"]["summary_version"] == 6
    assert fake_repo.recorded == [("srcrepo", 6)]


@pytest.mark.asyncio
async def test_file_job_no_chunks_still_resolves_and_finalizes_clean(tmp_path, fake_repo, monkeypatch):
    _pin(monkeypatch, 7)
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", True)
    monkeypatch.setattr(svc, "summary_vectors_supported", lambda: True)
    monkeypatch.setattr(svc.indexer, "parse_and_chunk", lambda fp: [])
    file_path = str(tmp_path / "a.py")
    job = svc._new_job("file", "srcfile", file_path)
    await svc._run_index_file_job(job, file_path)
    assert job["status"] == "done"
    assert job["payload"]["summary_version"] == 7
    assert fake_repo.recorded == [("srcfile", None)]
