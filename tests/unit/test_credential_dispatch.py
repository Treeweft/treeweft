"""A queued job whose backend credential is rejected fails at start
(specs/005-backend-credential-preflight, FR-005/FR-008).

This is the path the push webhook takes: it cannot be refused with a 409, so
its job is queued and fails here with a reason, recoverable by retry. Mirrors
the parser gate's runner tests.
"""
from __future__ import annotations

import asyncio

import pytest

from treeweft.application import index_guard as ig
from treeweft.application import indexer_runners as runners
from treeweft.application import indexer_state as state
from treeweft.domain.credentials import BackendAuthError, BackendStatus, CredentialState
from treeweft.domain.jobs import Job


def _backend(state_: CredentialState) -> BackendStatus:
    return BackendStatus(
        roles=("embedding",), state=state_, host="api.openai.com",
        model="text-embedding-3-small", settings=("OPENAI_API_KEY",),
        status_code=401 if state_ is CredentialState.AUTH_FAILED else 200,
    )


def _file_job(tmp_path, monkeypatch):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    ran: list = []

    async def fake_run(jd, target):
        ran.append(target)

    persisted: list = []

    async def fake_persist(jd):
        persisted.append(dict(jd))

    monkeypatch.setattr(runners, "_run_index_file_job", fake_run)
    monkeypatch.setattr(runners, "_persist_job", fake_persist)
    monkeypatch.setattr(state, "_jobs", {})
    return Job(id="j1", kind="file", source_path=str(f)), ran, persisted


def _backends(monkeypatch, state_):
    async def check(force=False):
        return [_backend(state_)]

    monkeypatch.setattr(ig, "check_backends", check)


def test_queued_job_fails_with_the_reason_and_takes_the_exception_path(monkeypatch, tmp_path):
    job, ran, persisted = _file_job(tmp_path, monkeypatch)
    _backends(monkeypatch, CredentialState.AUTH_FAILED)

    with pytest.raises(BackendAuthError, match="embedding_auth_failed"):
        asyncio.run(runners.dispatch_job(job))

    assert ran == []  # the runner never started
    assert persisted[-1]["status"] == "failed"
    assert persisted[-1]["error"].startswith("embedding_auth_failed:")
    assert persisted[-1]["finished_at"]


def test_valid_credentials_run_the_job(monkeypatch, tmp_path):
    job, ran, persisted = _file_job(tmp_path, monkeypatch)
    _backends(monkeypatch, CredentialState.VALID)
    asyncio.run(runners.dispatch_job(job))
    assert len(ran) == 1 and persisted == []


def test_an_unreachable_backend_does_not_fail_the_job(monkeypatch, tmp_path):
    job, ran, _ = _file_job(tmp_path, monkeypatch)
    _backends(monkeypatch, CredentialState.UNREACHABLE)
    asyncio.run(runners.dispatch_job(job))
    assert len(ran) == 1


def test_webhook_is_queued_not_refused(monkeypatch):
    """`POST /webhook` has no credential gate: a 409 would drop the push."""
    import inspect

    from treeweft.application import routes_webhook

    assert "require_credentials" not in inspect.getsource(routes_webhook)
