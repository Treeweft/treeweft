"""The embeddings adapter raises a typed, body-free error on a rejected
credential (specs/005, FR-007/FR-012, research R6/R8)."""
from __future__ import annotations

import httpx
import pytest

from treeweft.adapters.openai_api import embedding_adapter as ad
from treeweft.domain.credentials import BackendAuthError

pytestmark = pytest.mark.asyncio

KEY = "sk-adapter-0123456789abcdef"


def _client(status, body):
    def handler(request):
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def adapter(monkeypatch):
    monkeypatch.setattr(ad, "EMBEDDING_API_KEY", KEY)
    monkeypatch.setattr(ad, "EMBEDDING_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(ad, "EMBED_RETRIES", 3)


async def test_401_raises_backend_auth_error_without_retrying_or_leaking(monkeypatch, caplog):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {KEY[:12]}***"}})

    monkeypatch.setattr(ad, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with caplog.at_level("DEBUG"), pytest.raises(BackendAuthError) as info:
        await ad.embed(["x"])

    assert len(calls) == 1  # 4xx is not retried
    assert info.value.status == 401 and info.value.host == "api.openai.com"
    blob = str(info.value) + caplog.text + repr(info.value.__cause__)
    for length in range(8, len(KEY) + 1):
        assert KEY[:length] not in blob


async def test_403_about_the_model_is_not_a_credential_error(monkeypatch):
    monkeypatch.setattr(ad, "_client", _client(403, {"error": {"message": "no access to model x"}}))
    with pytest.raises(httpx.HTTPStatusError):
        await ad.embed(["x"])


async def test_a_400_still_raises_the_http_error(monkeypatch):
    monkeypatch.setattr(ad, "_client", _client(400, {"error": {"message": "bad input"}}))
    with pytest.raises(httpx.HTTPStatusError):
        await ad.embed(["x"])
