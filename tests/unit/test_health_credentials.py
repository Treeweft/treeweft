"""`/health` credential view: role and state only, from cache, never probes
(specs/005-backend-credential-preflight, User Story 3, FR-010, Principle VI)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.unit._credential_fakes import Clock, FakeBackends
from treeweft.application import index_guard as ig
from treeweft.application.indexer_service import app

pytestmark = pytest.mark.real_credential_check

KEY = "sk-health-0123456789abcdef"  # gitleaks:allow (fake test key)
client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    for name in ("EMBEDDING_API_KEY", "LLM_API_KEY", "LLM_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("USE_SUMMARY_VECTOR", "0")
    ig.reset_credential_cache()
    Clock().install(monkeypatch)


def _creds():
    resp = client.get("/health")
    assert resp.status_code == 200
    return resp.json()["credentials"], resp.text


def _by_role(creds):
    return {b["role"]: b for b in creds["backends"]}


def test_before_any_check_a_required_backend_is_unchecked_and_health_does_not_probe(monkeypatch):
    fake = FakeBackends().install(monkeypatch)
    creds, _ = _creds()
    assert _by_role(creds)["embedding"]["state"] == "unchecked"
    assert _by_role(creds)["summarizer"]["state"] == "skipped"
    assert creds["ready_for_indexing"] is True
    assert fake.requests == []  # /health never triggers a probe


@pytest.mark.asyncio
async def test_valid_then_auth_failed_then_unreachable(monkeypatch):
    fake = FakeBackends().install(monkeypatch)
    clock = ig._cred_clock

    await ig.check_backends()
    creds, _ = _creds()
    assert _by_role(creds)["embedding"]["state"] == "valid"
    assert _by_role(creds)["embedding"]["checked_at"].endswith("Z")

    fake.set("api.openai.com", 401)
    clock.now += 400
    await ig.check_backends()
    creds, _ = _creds()
    assert _by_role(creds)["embedding"]["state"] == "auth_failed"
    assert creds["ready_for_indexing"] is False

    fake.set("api.openai.com", None)
    clock.now += 60
    await ig.check_backends()
    creds, _ = _creds()
    assert _by_role(creds)["embedding"]["state"] == "unreachable"
    assert creds["ready_for_indexing"] is True  # an outage is not a bad key


@pytest.mark.asyncio
async def test_public_health_carries_no_host_model_reason_or_key(monkeypatch):
    FakeBackends({"api.openai.com": 401}).install(monkeypatch)
    await ig.check_backends()
    creds, raw = _creds()
    assert set(creds["backends"][0]) == {"role", "state", "checked_at"}
    assert "api.openai.com" not in raw
    assert "text-embedding" not in raw
    for length in range(4, len(KEY) + 1):
        assert KEY[:length] not in raw
