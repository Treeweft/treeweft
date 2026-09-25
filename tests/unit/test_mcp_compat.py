"""treeweft-mcp detects an incompatible indexer lazily, per call (ADR-004 §2)."""
import asyncio

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from treeweft.application import mcp_compat

pytestmark = pytest.mark.real_compat_check


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _resp(status, payload=None, text=""):
    request = httpx.Request("GET", "http://indexer/health")
    if payload is not None:
        return httpx.Response(status, json=payload, request=request)
    return httpx.Response(status, text=text, request=request)


class _FakeClient:
    """Stands in for httpx.AsyncClient; records calls; returns scripted results."""

    def __init__(self, script, calls):
        self._script, self._calls = script, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _next(self, method, url):
        self._calls.append((method, url))
        result = self._script[method].pop(0) if isinstance(self._script[method], list) else self._script[method]
        if isinstance(result, Exception):
            raise result
        return result

    async def get(self, url, params=None, headers=None):
        return await self._next("GET", url)

    async def post(self, url, json=None, headers=None):
        return await self._next("POST", url)


@pytest.fixture
def fake_http(monkeypatch):
    calls: list = []
    script: dict = {"GET": _resp(200, {"status": "ok", "version": "1.4.0"}), "POST": _resp(200, {})}

    def _factory(*a, **k):
        return _FakeClient(script, calls)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return script, calls


# ── incompatibility() ──────────────────────────────────────────────

def test_same_major_is_compatible():
    assert mcp_compat.incompatibility({"version": "1.9.2"}, client_version="1.4.0") is None


def test_different_major_names_both_versions_and_the_fix():
    err = mcp_compat.incompatibility({"version": "2.1.0"}, client_version="1.4.0")
    assert err == (
        "Incompatible indexer: indexer is 2.1.0, this treeweft-mcp is 1.4.0 (major "
        "versions must match). Upgrade treeweft-mcp to 2.x, or run an indexer 1.x."
    )


def test_indexer_without_version_predates_reporting():
    err = mcp_compat.incompatibility({"status": "ok"}, client_version="1.4.0")
    assert "predates version reporting" in err
    assert "1.x" in err


def test_unparseable_indexer_version():
    err = mcp_compat.incompatibility({"version": "2026.9.23"}, client_version="1.4.0")
    assert "unrecognised version '2026.9.23'" in err


# ── compat_error(): caching and failure handling ───────────────────

@pytest.mark.asyncio
async def test_success_is_cached_for_the_ttl(fake_http):
    script, calls = fake_http
    clock = _Clock()
    state = mcp_compat.CompatState(clock=clock)

    assert await mcp_compat.compat_error("http://indexer", state) is None
    assert await mcp_compat.compat_error("http://indexer", state) is None
    assert calls == [("GET", "http://indexer/health")]  # second call served from cache

    clock.now += mcp_compat.CACHE_TTL_SECONDS + 1
    assert await mcp_compat.compat_error("http://indexer", state) is None
    assert len(calls) == 2  # re-checked after the TTL


@pytest.mark.asyncio
async def test_mismatch_is_reported_and_not_cached(fake_http):
    script, calls = fake_http
    script["GET"] = _resp(200, {"version": "99.0.0"})
    state = mcp_compat.CompatState(clock=_Clock())

    first = await mcp_compat.compat_error("http://indexer", state)
    second = await mcp_compat.compat_error("http://indexer", state)

    assert first is not None and first.startswith("Incompatible indexer: indexer is 99.0.0")
    assert second == first
    assert len(calls) == 2  # an upgraded indexer is noticed on the very next call


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    httpx.ConnectError("Connection refused"),
    _resp(500, text="boom"),
])
async def test_unreachable_or_failing_health_is_not_an_error_and_not_cached(fake_http, failure):
    script, calls = fake_http
    script["GET"] = [failure, _resp(200, {"version": "1.0.0"})]
    state = mcp_compat.CompatState(clock=_Clock())

    assert await mcp_compat.compat_error("http://indexer", state) is None
    assert not state.is_fresh()
    assert await mcp_compat.compat_error("http://indexer", state) is None
    assert state.is_fresh()


@pytest.mark.asyncio
async def test_non_object_health_is_not_cached_and_not_an_error(fake_http):
    script, calls = fake_http
    script["GET"] = _resp(200, ["not", "an", "object"])
    state = mcp_compat.CompatState(clock=_Clock())

    assert await mcp_compat.compat_error("http://indexer", state) is None
    assert not state.is_fresh()


# ── describe_http_error() ──────────────────────────────────────────

def test_status_error_reports_code_and_detail():
    resp = _resp(409, {"detail": "index requires rebuild"})
    exc = httpx.HTTPStatusError("conflict", request=resp.request, response=resp)
    assert mcp_compat.describe_http_error(exc) == "Indexer returned HTTP 409: index requires rebuild"


def test_status_error_without_json_uses_the_body_text():
    resp = _resp(502, text="bad gateway")
    exc = httpx.HTTPStatusError("bad", request=resp.request, response=resp)
    assert mcp_compat.describe_http_error(exc) == "Indexer returned HTTP 502: bad gateway"


def test_connection_error_is_unreachable():
    assert mcp_compat.describe_http_error(httpx.ConnectError("refused")) == "Indexer unreachable: refused"


# ── wired into the MCP tools ───────────────────────────────────────

@pytest.fixture
def fresh_state(monkeypatch):
    state = mcp_compat.CompatState(clock=_Clock())
    monkeypatch.setattr(mcp_compat, "STATE", state)
    return state


@pytest.mark.asyncio
async def test_search_code_returns_dict_error_on_major_mismatch(fake_http, fresh_state):
    from treeweft.application import mcp_server

    script, calls = fake_http
    script["GET"] = _resp(200, {"version": "99.0.0"})

    result = await mcp_server.search_code(query="auth", source_id="repoA")

    assert isinstance(result, dict)
    assert result["error"].startswith("Incompatible indexer: indexer is 99.0.0")
    assert [m for m, _ in calls] == ["GET"]  # the search endpoint was never called


@pytest.mark.asyncio
async def test_list_tool_returns_list_error_on_major_mismatch(fake_http, fresh_state):
    from treeweft.application import mcp_server

    script, _ = fake_http
    script["GET"] = _resp(200, {"version": "99.0.0"})

    result = await mcp_server.find_definition(name="foo", source_id="repoA")

    assert isinstance(result, list) and result[0]["error"].startswith("Incompatible indexer")


@pytest.mark.asyncio
async def test_status_error_from_a_tool_is_not_reported_as_unreachable(fake_http, fresh_state):
    from treeweft.application import mcp_server

    script, _ = fake_http
    conflict = _resp(409, {"detail": "index requires rebuild"})
    script["POST"] = conflict

    result = await mcp_server.index_file(file_path="/repo/a.py")

    assert result == {"error": "Indexer returned HTTP 409: index requires rebuild"}


def test_every_tool_is_wrapped_and_its_schema_is_unchanged():
    """The decorator must not alter the MCP contract (names, input/output schemas)."""
    from treeweft.application import mcp_server

    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert len(tools) == 20
    scratch = FastMCP("scratch")
    for tool in tools:
        fn = getattr(mcp_server, tool.name)
        assert hasattr(fn, "__wrapped__"), f"{tool.name} is missing @_requires_compatible_indexer"
        scratch.tool(name=tool.name)(fn.__wrapped__)
    unwrapped = {t.name: t for t in asyncio.run(scratch.list_tools())}
    for tool in tools:
        assert tool.inputSchema == unwrapped[tool.name].inputSchema, tool.name
        assert tool.outputSchema == unwrapped[tool.name].outputSchema, tool.name
