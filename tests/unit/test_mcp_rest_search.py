"""The HTTP+SSE MCP container's REST `POST /search` (application/main.py).

It wraps the `search_code` tool, which returns markdown text unless asked for
JSON. The route read `.chunks` off that text, so every call answered
`{"error": "'str' object has no attribute 'chunks'"}`. These tests run the real
`search_code` and fake only the indexer's HTTP answer, so they pin the contract
between the two rather than a mock of it.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("INDEXER_URL", "http://indexer.test")

import httpx
from fastapi.testclient import TestClient

from treeweft.application import main, mcp_server

_CHUNK = {
    "file_path": "src/app/breaker.py",
    "start_line": 10,
    "end_line": 24,
    "snippet": "class CircuitBreaker:\n    ...",
    "score": 0.87,
    "language": "python",
}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Indexer:
    def __init__(self, captured, *, fail=False):
        self._captured = captured
        self._fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self._captured["url"] = url
        self._captured["json"] = json
        if self._fail:
            raise httpx.ConnectError("connection refused")
        return _Resp({"chunks": [_CHUNK], "neighbors": [], "community_summaries": {}})


@pytest.fixture
def indexer(mocker):
    captured: dict = {"fail": False}

    async def _compatible(_url):
        return None

    mocker.patch.object(mcp_server.mcp_compat, "compat_error", _compatible)
    mocker.patch.object(
        mcp_server.httpx,
        "AsyncClient",
        lambda *a, **k: _Indexer(captured, fail=captured["fail"]),
    )
    return captured


def test_rest_search_returns_the_indexer_chunks(indexer):
    resp = TestClient(main.app).post(
        "/search", json={"query": "circuit breaker", "repo": "src/app", "top_k": 3}
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "chunks": [
            {
                "file_path": "src/app/breaker.py",
                "start_line": 10,
                "end_line": 24,
                "snippet": "class CircuitBreaker:\n    ...",
                "score": 0.87,
            }
        ]
    }
    assert indexer["url"].endswith("/search")
    assert indexer["json"]["query"] == "circuit breaker"
    assert indexer["json"]["top_k"] == 3
    assert indexer["json"]["path_prefix"] == "src/app"


def test_rest_search_reports_an_unreachable_indexer(indexer):
    indexer["fail"] = True

    resp = TestClient(main.app).post("/search", json={"query": "circuit breaker"})

    body = resp.json()
    assert set(body) == {"error"}
    assert "chunks" not in body["error"]
