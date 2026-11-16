"""A running job stops fast when its backend credential is rejected
(specs/005-backend-credential-preflight, User Story 2, FR-007/FR-008).

The incident: a wrong embeddings key made every file fail fast, the job was
accepted and stayed `running`, 10 of 37,019 files in ten minutes. These tests
run the real walk against a fake embedder that answers 401.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from treeweft.application import indexer_runners as svc
from treeweft.application import indexer_state as _state
from treeweft.application import prompt_pins
from treeweft.domain.credentials import BackendAuthError

pytestmark = pytest.mark.asyncio

KEY = "sk-incident-key-1234567890"  # gitleaks:allow (fake test key)
PY = "def f():\n    return 1\n"


class _FakeSourceRepo:
    async def save(self, record):
        return None

    async def get_by_id(self, source_id):
        return None

    async def record_summary_version(self, source_id, version):
        return None

    async def set_graph_indexed(self, *args, **kwargs):
        return None


class _Embedder:
    """Answers 401 for every call (or per a script), counting calls."""

    def __init__(self, fail_on=None):
        self.calls = 0
        self.fail_on = fail_on  # None: always fail; else a set of 1-based call numbers

    async def __call__(self, texts):
        self.calls += 1
        if self.fail_on is None or self.calls in self.fail_on:
            raise BackendAuthError("embedding", "api.openai.com", "text-embedding-3-small", 401)
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


@pytest.fixture
def env(monkeypatch):
    stored: list = []

    async def insert_chunks(chunks, embeddings, summary_embeddings=None):
        stored.extend(chunks)

    monkeypatch.setattr(svc.graph_store, "store_graph", AsyncMock())
    monkeypatch.setattr(svc.graph_store, "upsert_source", AsyncMock())
    monkeypatch.setattr(svc, "insert_chunks", insert_chunks)
    monkeypatch.setattr(svc, "init_collection", AsyncMock())
    monkeypatch.setattr(svc, "_reset_source", AsyncMock())
    monkeypatch.setattr(svc, "_persist_job", AsyncMock())
    monkeypatch.setattr(svc, "_persist_job_sync", lambda job: None)
    monkeypatch.setattr(svc.community, "run_post_index_signals", AsyncMock(return_value=0))
    monkeypatch.setattr(_state, "_job_file_error_store", AsyncMock())
    monkeypatch.setattr(_state, "_source_repo", _FakeSourceRepo())
    monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: 4)
    monkeypatch.setattr(svc, "USE_SUMMARY_VECTOR", False)
    monkeypatch.setenv("TREEWEFT_CREDENTIAL_FAIL_STREAK", "3")
    return stored


def _tree(root, n):
    for i in range(n):
        (root / f"m{i:03d}.py").write_text(PY)


async def _run_dir(tmp_path):
    job = svc._new_job("directory", "src-fail", str(tmp_path))
    await svc._run_index_directory_job(job, str(tmp_path), "**/*")
    return job


async def test_incident_every_embed_call_401s_job_stops_after_the_streak(tmp_path, env, monkeypatch):
    embedder = _Embedder()
    monkeypatch.setattr(svc, "embed", embedder)
    _tree(tmp_path, 60)

    job = await _run_dir(tmp_path)

    assert job["status"] == "failed"
    assert job["error"].startswith("embedding_auth_failed:")
    # Bounded: the threshold plus files already past the abort check, never all 60.
    assert embedder.calls <= 3 + svc.INDEX_FILE_CONCURRENCY
    assert embedder.calls < 60
    assert job["processed_files"] < job["total_files"]


async def test_files_not_reached_are_not_counted_as_errors(tmp_path, env, monkeypatch):
    monkeypatch.setattr(svc, "INDEX_FILE_CONCURRENCY", 1)
    embedder = _Embedder()
    monkeypatch.setattr(svc, "embed", embedder)
    _tree(tmp_path, 20)

    job = await _run_dir(tmp_path)

    assert embedder.calls == 3  # exactly the streak, one file at a time
    assert job["errors"] == 3  # only files that actually failed
    assert job["processed_files"] == 3  # the other 17 are left for a retry


async def test_a_success_between_failures_resets_the_streak(tmp_path, env, monkeypatch):
    monkeypatch.setattr(svc, "INDEX_FILE_CONCURRENCY", 1)
    # Fails on calls 1, 3, 5: never three in a row, so the job must complete.
    embedder = _Embedder(fail_on={1, 3, 5})
    monkeypatch.setattr(svc, "embed", embedder)
    _tree(tmp_path, 8)

    job = await _run_dir(tmp_path)

    assert job["status"] != "failed", job.get("error")
    assert embedder.calls == 8


async def test_a_fixed_key_lets_a_fresh_run_finish(tmp_path, env, monkeypatch):
    monkeypatch.setattr(svc, "INDEX_FILE_CONCURRENCY", 1)
    monkeypatch.setattr(svc, "embed", _Embedder())
    _tree(tmp_path, 6)
    failed = await _run_dir(tmp_path)
    assert failed["status"] == "failed"

    monkeypatch.setattr(svc, "embed", _Embedder(fail_on=set()))  # the key is fixed
    retried = await _run_dir(tmp_path)

    assert retried["status"] != "failed", retried.get("error")
    assert retried["processed_files"] == retried["total_files"] == 6


async def test_single_file_job_fails_with_the_reason_code(tmp_path, env, monkeypatch):
    monkeypatch.setattr(svc, "embed", _Embedder())
    f = tmp_path / "only.py"
    f.write_text(PY)
    job = svc._new_job("file", "src-one", str(f))

    await svc._run_index_file_job(job, str(f))

    assert job["status"] == "failed"
    assert job["error"].startswith("embedding_auth_failed:")


async def test_job_error_and_logs_never_contain_the_key(tmp_path, env, monkeypatch, caplog):
    monkeypatch.setattr(svc, "embed", _Embedder())
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    _tree(tmp_path, 10)

    with caplog.at_level("DEBUG"):
        job = await _run_dir(tmp_path)

    blob = job["error"] + "\n".join(r.getMessage() for r in caplog.records)
    for length in range(8, len(KEY) + 1):
        assert KEY[:length] not in blob  # no prefix of the key


async def test_incremental_job_stops_on_a_credential_streak(tmp_path, env, monkeypatch):
    """The incremental runner swallowed per-file errors the same way
    (`files_failed += 1`), so a bad key made a webhook job grind through every
    changed file. It now fails after the streak and re-raises for retry."""
    import shutil

    src = tmp_path / "upstream"
    src.mkdir()
    for i in range(30):
        (src / f"m{i:03d}.py").write_text(PY)

    def fake_clone(url, path, **kwargs):
        shutil.copytree(src, path, dirs_exist_ok=True)

    embedder = _Embedder()
    monkeypatch.setattr(svc, "embed", embedder)
    monkeypatch.setattr(svc.Repo, "clone_from", staticmethod(fake_clone))
    monkeypatch.setattr(svc, "_graph_index_file", AsyncMock())
    monkeypatch.setattr(svc, "_finalize_incremental_job", AsyncMock())
    monkeypatch.setattr(svc, "delete_chunks_by_file", lambda *a, **k: None)
    changed = [{"path": f"m{i:03d}.py", "action": "added"} for i in range(30)]
    job = svc._new_job("incremental", "src-inc", "https://example.com/r.git")

    with pytest.raises(BackendAuthError):
        await svc._run_incremental_index(
            "src-inc", "https://example.com/r.git", "main", changed, job
        )

    assert job["status"] == "failed"
    assert job["error"].startswith("embedding_auth_failed:")
    assert embedder.calls == 3  # the streak, one file at a time (sequential loop)
