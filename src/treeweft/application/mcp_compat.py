"""treeweft-mcp ↔ indexer compatibility (ADR-004 §2).

The check is lazy: nothing is contacted at startup, so an indexer that is down
when the agent launches never removes Treeweft's tools from the session. Each
tool call first asks `compat_error()`; a successful check is cached for
CACHE_TTL_SECONDS, while a mismatch or an unreachable indexer is re-checked on
the next call. No environment variables and no I/O at import.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from treeweft.versions import SOURCE_VERSION, parse_semver

CACHE_TTL_SECONDS = 300.0


@dataclass
class CompatState:
    ttl: float = CACHE_TTL_SECONDS
    clock: Callable[[], float] = field(default=time.monotonic)
    ok_until: float = 0.0

    def is_fresh(self) -> bool:
        return self.clock() < self.ok_until

    def mark_ok(self) -> None:
        self.ok_until = self.clock() + self.ttl


STATE = CompatState()


def incompatibility(health: dict, client_version: str = SOURCE_VERSION) -> str | None:
    """Return an error message if the indexer behind `health` can't serve this client."""
    client_major = parse_semver(client_version)[0]
    indexer_version = health.get("version")
    if not indexer_version:
        return (
            "Incompatible indexer: it predates version reporting (no 'version' in /health). "
            f"Upgrade the indexer to {client_major}.x to use this treeweft-mcp {client_version}."
        )
    try:
        indexer_major = parse_semver(indexer_version)[0]
    except ValueError:
        return f"Incompatible indexer: unrecognised version {indexer_version!r} in /health."
    if indexer_major != client_major:
        return (
            f"Incompatible indexer: indexer is {indexer_version}, this treeweft-mcp is "
            f"{client_version} (major versions must match). Upgrade treeweft-mcp to "
            f"{indexer_major}.x, or run an indexer {client_major}.x."
        )
    return None


async def compat_error(indexer_url: str, state: CompatState | None = None) -> str | None:
    """None when the tool may proceed; otherwise the error the tool must return.

    An unreachable, failing or garbled /health returns None without caching:
    the tool then makes its own request and reports that failure normally.
    """
    state = state if state is not None else STATE
    if state.is_fresh():
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{indexer_url}/health")
            resp.raise_for_status()
            health = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(health, dict):
        return None
    error = incompatibility(health)
    if error is None:
        state.mark_ok()
    return error


def describe_http_error(exc: httpx.HTTPError) -> str:
    """Tell an HTTP error response apart from an unreachable indexer."""
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        if detail is None:
            detail = resp.text[:300]
        return f"Indexer returned HTTP {resp.status_code}: {detail}"
    return f"Indexer unreachable: {exc}"
