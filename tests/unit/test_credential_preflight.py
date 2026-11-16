"""The credential check: caching, single-flight, de-duplication, refusal body
(specs/005-backend-credential-preflight, FR-001/002/006/009/012)."""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.unit._credential_fakes import Clock, FakeBackends
from treeweft.application import index_guard as ig
from treeweft.domain.credentials import CredentialState

pytestmark = [pytest.mark.real_credential_check, pytest.mark.asyncio]

KEY = "sk-live-0123456789abcdef"  # gitleaks:allow (fake test key)
EMBED_HOST = "api.openai.com"


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    for name in (
        "EMBEDDING_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY", "EMBEDDING_BASE_URL",
        "LLM_URL", "LLM_MODEL", "USE_SUMMARY_VECTOR",
        "TREEWEFT_CREDENTIAL_CHECK_SECONDS", "TREEWEFT_CREDENTIAL_RETRY_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("USE_SUMMARY_VECTOR", "0")
    ig.reset_credential_cache()


@pytest.fixture
def backends(monkeypatch):
    return FakeBackends().install(monkeypatch)


@pytest.fixture
def clock(monkeypatch):
    return Clock().install(monkeypatch)


async def test_a_pass_is_reused_inside_the_ttl_and_reprobed_after(backends, clock):
    await ig.check_backends()
    await ig.check_backends()
    assert backends.calls_to(EMBED_HOST) == 1
    clock.now += 301  # default TTL is 300
    await ig.check_backends()
    assert backends.calls_to(EMBED_HOST) == 2


async def test_a_failure_is_rechecked_after_the_short_interval(backends, clock):
    backends.set(EMBED_HOST, [401, 200])
    first = await ig.check_backends()
    assert first[0].state is CredentialState.AUTH_FAILED
    clock.now += 10  # inside the 30 s retry interval: still the cached failure
    assert (await ig.check_backends())[0].state is CredentialState.AUTH_FAILED
    assert backends.calls_to(EMBED_HOST) == 1
    clock.now += 25  # past it: a corrected key is accepted without a restart
    assert (await ig.check_backends())[0].state is CredentialState.VALID
    assert backends.calls_to(EMBED_HOST) == 2


async def test_concurrent_callers_share_one_probe(backends, clock):
    await asyncio.gather(*(ig.check_backends() for _ in range(10)))
    assert backends.calls_to(EMBED_HOST) == 1


async def test_embedding_and_summarizer_sharing_a_key_make_one_probe(monkeypatch, backends, clock):
    monkeypatch.setenv("USE_SUMMARY_VECTOR", "1")
    monkeypatch.setenv("LLM_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    results = await ig.check_backends()
    assert backends.calls_to(EMBED_HOST) == 1
    assert len(results) == 1
    assert set(results[0].roles) == {"embedding", "summarizer"}


async def test_different_keys_on_different_hosts_are_probed_separately(monkeypatch, backends, clock):
    monkeypatch.setenv("USE_SUMMARY_VECTOR", "1")
    monkeypatch.setenv("LLM_URL", "https://llm.example.org/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_API_KEY", "llm-key-0000")
    await ig.check_backends()
    assert backends.calls_to(EMBED_HOST) == 1
    assert backends.calls_to("llm.example.org") == 1


@pytest.mark.parametrize("status", [None, 429, 500, 503, 404])
async def test_unreachable_never_refuses(backends, clock, status):
    backends.set(EMBED_HOST, status)
    assert await ig.require_credentials() is None


async def test_rejected_key_refuses_with_a_409_naming_the_backend_not_the_key(backends, clock):
    backends.set(EMBED_HOST, 401)
    resp = await ig.require_credentials()
    assert resp is not None and resp.status_code == 409
    body = json.loads(resp.body)
    assert body["reason"] == "embedding_auth_failed"
    assert EMBED_HOST in body["detail"] and "OPENAI_API_KEY" in body["detail"]
    assert body["backends"][0]["roles"] == ["embedding"]
    assert body["backends"][0]["settings"] == ["OPENAI_API_KEY"]
    raw = resp.body.decode()
    for length in range(4, len(KEY) + 1):
        assert KEY[:length] not in raw


async def test_summarizer_failure_blocks_only_when_summaries_are_enabled(monkeypatch, backends, clock):
    monkeypatch.setenv("LLM_URL", "https://llm.example.org/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_API_KEY", "llm-key-0000")
    backends.set("llm.example.org", 401)

    monkeypatch.setenv("USE_SUMMARY_VECTOR", "0")
    assert await ig.require_credentials() is None  # not probed, cannot block
    assert backends.calls_to("llm.example.org") == 0

    monkeypatch.setenv("USE_SUMMARY_VECTOR", "1")
    ig.reset_credential_cache()
    resp = await ig.require_credentials()
    assert resp is not None
    assert json.loads(resp.body)["reason"] == "summarizer_auth_failed"


async def test_a_403_about_the_model_is_not_a_bad_key(backends, clock, monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(403, json={"error": {"message": "You do not have access to model x"}})

    from tests.unit import _credential_fakes as f

    monkeypatch.setattr(
        f.credential_probe.httpx, "AsyncClient",
        lambda **kw: f._RealAsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await ig.require_credentials() is None


async def test_a_probe_bug_never_blocks_indexing(monkeypatch, backends, clock):
    async def boom(group):
        raise RuntimeError("probe bug")

    monkeypatch.setattr(ig, "_probe_group", boom)
    assert await ig.require_credentials() is None


async def test_the_state_change_is_logged_once(backends, clock, caplog):
    backends.set(EMBED_HOST, 401)
    with caplog.at_level("WARNING"):
        for _ in range(3):
            clock.now += 31
            await ig.check_backends()
    rejected = [r for r in caplog.records if "rejected its credential" in r.getMessage()]
    assert len(rejected) == 1
    assert KEY not in caplog.text
