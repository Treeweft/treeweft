"""Backends that need no key are never probed (specs/005, User Story 4, FR-011)
and the summarizer is probed only when summaries run during indexing (FR-006)."""
from __future__ import annotations

import pytest

from tests.unit._credential_fakes import Clock, FakeBackends
from treeweft.application import index_guard as ig
from treeweft.domain.credentials import CredentialState

pytestmark = [pytest.mark.real_credential_check, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    for name in ("EMBEDDING_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY", "LLM_URL", "LLM_MODEL", "EMBEDDING_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("USE_SUMMARY_VECTOR", "0")
    ig.reset_credential_cache()
    Clock().install(monkeypatch)


async def test_tei_provider_is_never_probed(monkeypatch):
    fake = FakeBackends().install(monkeypatch)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "tei")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-irrelevant-0000")  # present, but TEI takes no key
    assert await ig.check_backends() == []
    assert fake.requests == []
    assert ig.credential_health()["backends"][0]["state"] == "skipped"


async def test_openai_provider_without_a_key_is_key_free_and_skipped(monkeypatch):
    fake = FakeBackends().install(monkeypatch)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://localhost:8082/v1")  # a local server, no key
    assert await ig.require_credentials() is None
    assert fake.requests == []


async def test_summarizer_is_skipped_when_summaries_are_off(monkeypatch):
    fake = FakeBackends({"llm.example.org": 401}).install(monkeypatch)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "tei")
    monkeypatch.setenv("LLM_URL", "https://llm.example.org/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_API_KEY", "llm-key-0000")
    assert await ig.require_credentials() is None
    assert fake.calls_to("llm.example.org") == 0
    roles = {b["role"]: b["state"] for b in ig.credential_health()["backends"]}
    assert roles["summarizer"] == "skipped"


async def test_simple_profile_one_shared_key_is_one_probe(monkeypatch):
    fake = FakeBackends().install(monkeypatch)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-simple-0123456789")
    monkeypatch.setenv("USE_SUMMARY_VECTOR", "1")
    monkeypatch.setenv("LLM_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    results = await ig.check_backends()
    assert len(fake.requests) == 1
    assert results[0].state is CredentialState.VALID
