"""One-request credential probes for OpenAI-compatible backends.

Each probe makes the smallest real request the backend's own adapter makes
(one-token embeddings input, one-token chat completion), so it exercises the
key, base URL and model together. `GET /models` is deliberately not used:
many compatible servers lack it or do not authenticate it, so it can pass for
a bad key. See specs/005-backend-credential-preflight/research.md R3.

A probe never raises and never returns provider text: the outcome is a
`CredentialState` only (FR-002, FR-012).
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from treeweft.domain.credentials import CredentialState, classify

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 10.0


async def _post(
    url: str, key: str, payload: dict, client: httpx.AsyncClient | None
) -> tuple[CredentialState, int | None]:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    owns_client = client is None
    c = client or httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS)
    try:
        resp = await c.post(url, json=payload, headers=headers)
        return classify(resp.status_code, resp.text), resp.status_code
    except httpx.HTTPError as exc:
        # Timeout, DNS, TLS, connection refused: unreachable, never "bad key".
        logger.debug("credential probe to %s failed: %s", urlparse(url).netloc, type(exc).__name__)
        return CredentialState.UNREACHABLE, None
    finally:
        if owns_client:
            await c.aclose()


async def probe_embeddings(
    base_url: str, model: str, key: str, client: httpx.AsyncClient | None = None
) -> tuple[CredentialState, int | None]:
    return await _post(
        f"{base_url.rstrip('/')}/embeddings", key, {"model": model, "input": ["ping"]}, client
    )


async def probe_chat(
    llm_url: str, model: str, key: str, client: httpx.AsyncClient | None = None
) -> tuple[CredentialState, int | None]:
    return await _post(
        f"{llm_url.rstrip('/')}/chat/completions",
        key,
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
        client,
    )
