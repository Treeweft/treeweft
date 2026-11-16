"""Every parsing write route refuses with a 409 when a backend credential is
rejected, and creates no job (specs/005-backend-credential-preflight,
User Story 1, FR-004/FR-005).

Shares `PARSER_GATED_REQUESTS` with the parser gate: a route that parses code
is, by that list, also credential-gated, so adding a route to it extends both.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.unit.test_index_gate_routes import PARSER_GATED_REQUESTS, PARSER_EXEMPT, _no_admin_required
from treeweft.application import index_guard as ig
from treeweft.application import indexer_state as state
from treeweft.application.indexer_service import app
from treeweft.domain.credentials import BackendStatus, CredentialState

client = TestClient(app, raise_server_exceptions=False)


def _status(state_: CredentialState) -> BackendStatus:
    return BackendStatus(
        roles=("embedding",), state=state_, host="api.openai.com",
        model="text-embedding-3-small", settings=("OPENAI_API_KEY",),
        status_code=401 if state_ is CredentialState.AUTH_FAILED else None,
    )


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    monkeypatch.setattr("treeweft.application.indexer_service.authz._require_admin", _no_admin_required)


def _set_backend(monkeypatch, state_: CredentialState):
    async def check(force=False):
        return [_status(state_)]

    monkeypatch.setattr(ig, "check_backends", check)


def _call(monkeypatch, method, path, body):
    if path == "/jobs/{job_id}/retry":
        from treeweft.domain.jobs import Job, JobStatus

        job = Job(id="j1", kind="file", status=JobStatus.DEAD_LETTER)

        class _Store:
            async def get(self, job_id):
                return job

        monkeypatch.setattr(state, "_job_store", _Store())
        path = "/jobs/j1/retry"
    return client.request(method, path, json=body) if body is not None else client.request(method, path)


@pytest.mark.parametrize("method,path,body", PARSER_GATED_REQUESTS, ids=[p for _, p, _ in PARSER_GATED_REQUESTS])
def test_rejected_credential_refuses_every_gated_route(monkeypatch, method, path, body):
    _set_backend(monkeypatch, CredentialState.AUTH_FAILED)
    resp = _call(monkeypatch, method, path, body)
    assert resp.status_code == 409, resp.text
    payload = resp.json()
    assert payload["reason"] == "embedding_auth_failed"
    assert payload["backends"][0]["host"] == "api.openai.com"
    assert payload["backends"][0]["roles"] == ["embedding"]


@pytest.mark.parametrize("method,path,body", PARSER_GATED_REQUESTS, ids=[p for _, p, _ in PARSER_GATED_REQUESTS])
def test_an_unreachable_backend_does_not_refuse(monkeypatch, method, path, body):
    _set_backend(monkeypatch, CredentialState.UNREACHABLE)
    resp = _call(monkeypatch, method, path, body)
    assert not (resp.status_code == 409 and resp.json().get("reason") == "embedding_auth_failed"), resp.text


def test_a_rejected_credential_creates_no_job(monkeypatch):
    _set_backend(monkeypatch, CredentialState.AUTH_FAILED)
    before = dict(state._jobs)
    resp = client.post("/index-repo", json={"path": "/tmp/does-not-exist-repo"})
    assert resp.status_code == 409
    assert state._jobs == before


def test_the_webhook_is_exempt_and_documented():
    assert ("POST", "/webhook") in PARSER_EXEMPT  # a 409 would drop the push (FR-005)
    assert ("POST", "/webhook") not in {(m, p) for m, p, _ in PARSER_GATED_REQUESTS}
