"""treeweft-mcp detects an incompatible indexer lazily, per call (ADR-004 §2)."""
import asyncio
import json
import typing  # noqa: F401  (named in a string annotation below)
import logging

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
async def test_non_object_health_is_not_cached_and_not_an_error(fake_http, caplog):
    script, calls = fake_http
    script["GET"] = _resp(200, ["not", "an", "object"])
    state = mcp_compat.CompatState(clock=_Clock())

    with caplog.at_level(logging.WARNING, logger="treeweft.application.mcp_compat"):
        assert await mcp_compat.compat_error("http://indexer", state) is None
    assert not state.is_fresh()
    assert any(
        "non-object body; skipping the compatibility check" in record.getMessage()
        for record in caplog.records
    )


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


def test_status_error_detail_is_bounded():
    # A FastAPI 422 `detail` is a list of per-field validation errors that can
    # echo arbitrarily large input; the message must stay bounded.
    long_detail = [{"loc": ["body", "query"], "msg": "x" * 50, "type": "value_error"} for _ in range(20)]
    resp = _resp(422, {"detail": long_detail})
    exc = httpx.HTTPStatusError("unprocessable", request=resp.request, response=resp)
    message = mcp_compat.describe_http_error(exc)
    assert len(message) <= len("Indexer returned HTTP 422: ") + 300


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


@pytest.mark.asyncio
async def test_reindex_required_409_reaches_the_agent_with_the_rebuild_pointer(fake_http, fresh_state):
    """ADR-004 §3 FR-018: the errors.md 409 body reaches search_code as
    `detail`, with the rebuild pointer intact and never "unreachable" —
    exercising Plan 1's existing error mapping, not new MCP code."""
    from treeweft.application import mcp_server

    script, _ = fake_http
    body = {
        "detail": (
            "Index requires rebuild: vector store (milvus): embedding_model is BAAI/bge-m3, "
            "configured Qwen/Qwen3-Embedding-0.6B. Run POST /index/rebuild?dry_run=true, then "
            "POST /index/rebuild (docs/upgrading.md)."
        ),
        "reason": "vector store (milvus): embedding_model is BAAI/bge-m3, configured Qwen/Qwen3-Embedding-0.6B",
        "index_status": "reindex_required",
        "rebuild": "/index/rebuild",
    }
    script["POST"] = _resp(409, body)

    result = await mcp_server.search_code(query="auth", source_id="repoA")

    assert result == {"error": f"Indexer returned HTTP 409: {body['detail']}"}
    assert "/index/rebuild" in result["error"]
    assert "unreachable" not in result["error"].lower()


# ── an error has the shape the tool's output schema declares ─────────
# FastMCP validates what a tool returns against its output schema. A tool
# declared `-> list[dict]` that returns `{"error": ...}` does not reach the
# agent as that error: the agent gets "1 validation error ... Input should be
# a valid list".

def _tools() -> dict:
    from treeweft.application import mcp_server

    return {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}


def _returns_list(tool) -> bool:
    result = (tool.outputSchema or {}).get("properties", {}).get("result", {})
    return result.get("type") == "array"


def _arguments(tool) -> dict:
    """A value for each required argument, by its declared type."""
    values = {"string": "x", "integer": 1, "number": 1.0, "boolean": False, "array": [], "object": {}}
    properties = tool.inputSchema.get("properties", {})
    return {
        name: values[properties[name].get("type", "string")]
        for name in tool.inputSchema.get("required", [])
    }


TOOLS = _tools()  # read once, at import: asyncio.run() cannot run inside a test's event loop
LIST_TOOLS = sorted(name for name, tool in TOOLS.items() if _returns_list(tool))
ALL_TOOLS = sorted(TOOLS)


def test_the_list_returning_tools_are_the_ones_expected():
    assert LIST_TOOLS == [
        "find_callers", "find_definition", "find_references",
        "list_index_jobs", "list_indexed_sources",
    ]


def _error_of(result) -> str:
    """The error an agent reads in the result of a tool call: the structured
    result where the tool has an output schema, else the JSON text."""
    if isinstance(result, tuple):
        _content, structured = result
        payload = structured["result"] if set(structured) == {"result"} else structured
    else:
        payload = json.loads(result[0].text)
    if isinstance(payload, list):
        assert len(payload) == 1, payload
        payload = payload[0]
    return payload["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ALL_TOOLS)
async def test_major_mismatch_reaches_the_agent_from_every_tool(name, fake_http, fresh_state):
    from treeweft.application import mcp_server

    script, calls = fake_http
    script["GET"] = _resp(200, {"version": "99.0.0"})
    tool = TOOLS[name]

    result = await mcp_server.mcp.call_tool(name, _arguments(tool))

    assert _error_of(result).startswith("Incompatible indexer: indexer is 99.0.0")
    assert [m for m, _ in calls] == ["GET"]  # only the health check was made


@pytest.mark.asyncio
@pytest.mark.parametrize("name", LIST_TOOLS)
async def test_indexer_error_reaches_the_agent_from_every_list_tool(name, fake_http, fresh_state):
    from treeweft import versions
    from treeweft.application import mcp_server

    script, _ = fake_http
    failure = _resp(503, {"detail": "graph store unavailable"})
    script["GET"] = [_resp(200, {"status": "ok", "version": versions.SOURCE_VERSION}), failure]
    script["POST"] = failure
    tool = TOOLS[name]

    result = await mcp_server.mcp.call_tool(name, _arguments(tool))

    assert _error_of(result) == "Indexer returned HTTP 503: graph store unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", LIST_TOOLS)
async def test_list_tool_called_directly_returns_a_list_on_an_indexer_error(name, fake_http, fresh_state):
    from treeweft import versions
    from treeweft.application import mcp_server

    script, _ = fake_http
    script["GET"] = [
        _resp(200, {"status": "ok", "version": versions.SOURCE_VERSION}),
        _resp(503, {"detail": "graph store unavailable"}),
    ]

    result = await getattr(mcp_server, name)(**_arguments(TOOLS[name]))

    assert result == [{"error": "Indexer returned HTTP 503: graph store unavailable"}]


# ── the decorator reads the return type however the annotation is stored ──

async def _annotated_with_a_string() -> "list[dict]":
    return []


async def _annotated_with_a_string_dict() -> "dict":
    return {}


async def _annotated_with_bare_list() -> list:
    return []


async def _annotated_with_typing_list() -> "typing.List[dict]":
    return []


async def _not_annotated():
    return {}


async def _annotated_with_an_unknown_name() -> "NoSuchType":  # noqa: F821
    return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("fn,expected", [
    (_annotated_with_a_string, list),
    (_annotated_with_bare_list, list),
    (_annotated_with_typing_list, list),
    (_annotated_with_a_string_dict, dict),
    (_not_annotated, dict),
    (_annotated_with_an_unknown_name, dict),
], ids=lambda v: getattr(v, "__name__", None))
async def test_error_shape_follows_the_return_type_with_postponed_annotations(
    fn, expected, fake_http, fresh_state
):
    """`from __future__ import annotations` stores every annotation as a
    string. typing.get_origin("list[dict]") is None, so reading the raw
    annotation would send a dict from a tool declared to return a list."""
    import typing  # noqa: F401  (named in an annotation above)
    from treeweft.application import mcp_server

    script, _ = fake_http
    script["GET"] = _resp(200, {"version": "99.0.0"})

    result = await mcp_server._requires_compatible_indexer(fn)()

    assert type(result) is expected
    error = result[0]["error"] if expected is list else result["error"]
    assert error.startswith("Incompatible indexer")


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
