"""Shared fakes for the credential-check tests: a scripted HTTP transport (no
real provider is ever contacted) and an injectable clock."""
from __future__ import annotations

import httpx

from treeweft.adapters.openai_api import credential_probe
from treeweft.application import index_guard

_RealAsyncClient = httpx.AsyncClient


class FakeBackends:
    """Answers probe requests by URL host from a script, recording each call.

    `script[host]` is a status code (always), or a list consumed one per call
    (the last repeats). `None` raises a connect error (unreachable)."""

    def __init__(self, script: dict[str, int | list[int | None] | None] | None = None):
        self.script = script or {}
        self.requests: list[httpx.Request] = []

    def set(self, host: str, value) -> None:
        self.script[host] = value

    def _status_for(self, host: str):
        value = self.script.get(host, 200)
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self._status_for(request.url.host)
        if status is None:
            raise httpx.ConnectError("refused", request=request)
        body = {"error": {"message": "Incorrect API key provided: sk-secret12*****"}} if status in (401, 403) else {}
        return httpx.Response(status, json=body)

    def calls_to(self, host: str) -> int:
        return sum(1 for r in self.requests if r.url.host == host)

    def install(self, monkeypatch) -> "FakeBackends":
        def factory(*args, **kwargs):
            kwargs.pop("timeout", None)
            return _RealAsyncClient(transport=httpx.MockTransport(self._handler))

        monkeypatch.setattr(credential_probe.httpx, "AsyncClient", factory)
        return self


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def install(self, monkeypatch) -> "Clock":
        monkeypatch.setattr(index_guard, "_cred_clock", self)
        return self
