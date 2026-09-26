"""Versioned summaries and HyDE (T011, research R2, ADR-003).

`cache_get_many`/`cache_put`/`summarize_with_cache` take an explicit
prompt version instead of the old module-level `PROMPT_VERSION` constant, so
several versions' cache rows coexist (FR-007). A test-only v4 is monkeypatched
into `prompts.REGISTRY["chunk_summary"]` (and a v2 into `["hyde"]`) so the
whole path can be exercised without touching the real registry. Postgres is
faked with the `_FakePool`/`_FakeConn` pattern used elsewhere
(tests/unit/test_summary_rejection_cache.py:163-186).
"""
from __future__ import annotations

import pytest

from treeweft.adapters.llm_api import llm_adapter, llm_caller, prompts
from treeweft.application import prompt_pins

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fake Postgres pool for the summary cache
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self, store: dict[tuple[str, str, int], str]):
        self._store = store

    async def fetch(self, _sql, model, prompt_version, sha1s):
        return [
            {"sha1": sha1, "summary": summary}
            for (sha1, m, pv), summary in self._store.items()
            if m == model and pv == prompt_version and sha1 in sha1s
        ]

    async def execute(self, _sql, sha1, model, prompt_version, summary, _created_at):
        self._store[(sha1, model, prompt_version)] = summary


class _FakePool:
    def __init__(self, store: dict[tuple[str, str, int], str]):
        self._store = store

    def acquire(self):
        store = self._store

        class _Ctx:
            async def __aenter__(self):
                return _FakeConn(store)

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


@pytest.fixture
def fake_pool(monkeypatch):
    store: dict[tuple[str, str, int], str] = {}

    async def _get_pool():
        return _FakePool(store)

    monkeypatch.setattr(llm_adapter, "get_pool", _get_pool)
    return store


# ---------------------------------------------------------------------------
# A registered test v4 (chunk_summary) and v2 (hyde), distinct from the
# shipped versions so behaviour that depends on the resolved version is
# provable rather than incidentally true of the default.
# ---------------------------------------------------------------------------

_V4 = prompts.PromptVersion(
    operation="chunk_summary",
    version=4,
    system="v4 test system prompt.",
    schema=prompts.FrozenResponseSchema(min_length=1, max_length=10, forbidden_phrases=("nope4",)),
    notes="test v4",
)

_HYDE_V2 = prompts.PromptVersion(
    operation="hyde",
    version=2,
    system="v2 test hyde prompt.",
    schema=prompts.FrozenResponseSchema(min_length=1, max_length=8000, forbidden_phrases=()),
    notes="test hyde v2",
)


@pytest.fixture
def registry_v4(monkeypatch):
    registry = dict(prompts.REGISTRY)
    registry["chunk_summary"] = {**registry["chunk_summary"], 4: _V4}
    monkeypatch.setattr(prompts, "REGISTRY", registry)
    return _V4


@pytest.fixture
def registry_hyde_v2(monkeypatch):
    registry = dict(prompts.REGISTRY)
    registry["hyde"] = {**registry["hyde"], 2: _HYDE_V2}
    monkeypatch.setattr(prompts, "REGISTRY", registry)
    return _HYDE_V2


def _pin(monkeypatch, version: int):
    """Monkeypatch `prompt_pins.effective` to always resolve to `version`."""
    monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: version)


# ---------------------------------------------------------------------------
# cache_put: writes the caller's prompt_version
# ---------------------------------------------------------------------------

async def test_cache_put_writes_the_given_prompt_version(fake_pool):
    await llm_adapter.cache_put("sha1", "a summary", prompt_version=4)
    assert fake_pool[("sha1", llm_adapter.LLM_MODEL, 4)] == "a summary"


async def test_cache_put_requires_prompt_version_keyword(fake_pool):
    with pytest.raises(TypeError):
        await llm_adapter.cache_put("sha1", "a summary", 4)  # positional: rejected


# ---------------------------------------------------------------------------
# cache_get_many: prompt_version is required and filters rows by it
# ---------------------------------------------------------------------------

async def test_cache_get_many_requires_prompt_version(fake_pool):
    with pytest.raises(TypeError):
        await llm_adapter.cache_get_many(["sha1"])


async def test_cache_get_many_filters_on_prompt_version(fake_pool):
    await llm_adapter.cache_put("sha1", "v3 summary", prompt_version=3)
    await llm_adapter.cache_put("sha1", "v4 summary", prompt_version=4)
    assert await llm_adapter.cache_get_many(["sha1"], 3) == {"sha1": "v3 summary"}
    assert await llm_adapter.cache_get_many(["sha1"], 4) == {"sha1": "v4 summary"}
    assert await llm_adapter.cache_get_many(["sha1"], 99) == {}


# ---------------------------------------------------------------------------
# summarize_with_cache: (summary, strategy), version-aware, and only
# "rejected" writes the negative marker
# ---------------------------------------------------------------------------

async def test_summarize_with_cache_uses_v4_prompt_and_schema(fake_pool, registry_v4, monkeypatch):
    captured = {}

    async def _fake(*, messages, max_tokens, operation, validator=None, timeout=None,
                     timeout_includes_queue=True):
        captured["messages"] = messages
        captured["validator"] = validator
        return "A v4 summary.", "simple"

    monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)
    out = await llm_adapter.summarize_with_cache("def f(): pass", "python", "a.py", version=4)

    assert out == ("A v4 summary.", "generated")
    assert captured["messages"][0]["content"].startswith(_V4.system)
    # The v4 schema (max_length=10) is in effect, not v3's (max_length=500):
    # a longer-than-10 output fails validation under v4's schema.
    result = captured["validator"].validate("this text is far longer than ten characters")
    assert result.passed is False


async def test_summarize_with_cache_cached_skips_generation(fake_pool, registry_v4, monkeypatch):
    key = llm_adapter.chunk_cache_key("def f(): pass")
    fake_pool[(key, llm_adapter.LLM_MODEL, 4)] = "Cached summary."

    async def _boom(**_kw):
        raise AssertionError("a cache hit must not call the LLM")

    monkeypatch.setattr(llm_caller, "call_with_control_layer", _boom)
    out = await llm_adapter.summarize_with_cache("def f(): pass", "python", "a.py", version=4)
    assert out == ("Cached summary.", "cached")


async def test_summarize_with_cache_generated_writes_the_cache(fake_pool, registry_v4, monkeypatch):
    async def _fake(**_kw):
        return "A v4 summary.", "simple"

    monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)
    out = await llm_adapter.summarize_with_cache("def f(): pass", "python", "a.py", version=4)

    assert out == ("A v4 summary.", "generated")
    key = llm_adapter.chunk_cache_key("def f(): pass")
    assert fake_pool[(key, llm_adapter.LLM_MODEL, 4)] == "A v4 summary."


async def test_summarize_with_cache_rejected_writes_the_empty_marker(fake_pool, registry_v4, monkeypatch):
    async def _fake(**_kw):
        return None, "rejected"

    monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)
    out = await llm_adapter.summarize_with_cache("def f(): pass", "python", "a.py", version=4)

    assert out == (None, "rejected")
    key = llm_adapter.chunk_cache_key("def f(): pass")
    assert fake_pool[(key, llm_adapter.LLM_MODEL, 4)] == llm_adapter.REJECTED_SUMMARY


async def test_summarize_with_cache_error_writes_nothing(fake_pool, registry_v4, monkeypatch):
    async def _fake(**_kw):
        return None, "error"

    monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)
    out = await llm_adapter.summarize_with_cache("def f(): pass", "python", "a.py", version=4)

    assert out == (None, "error")
    key = llm_adapter.chunk_cache_key("def f(): pass")
    assert (key, llm_adapter.LLM_MODEL, 4) not in fake_pool


# ---------------------------------------------------------------------------
# generate_summary: still returns str | None, resolving the version itself
# ---------------------------------------------------------------------------

async def test_generate_summary_still_returns_str_or_none(fake_pool, registry_v4, monkeypatch):
    _pin(monkeypatch, 4)

    async def _fake(*, messages, max_tokens, operation, validator=None, timeout=None,
                     timeout_includes_queue=True):
        assert messages[0]["content"].startswith(_V4.system)
        return "A v4 summary.", "simple"

    monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)
    out = await llm_adapter.generate_summary("def f(): pass", "python", "a.py")
    assert out == "A v4 summary."


async def test_generate_summary_returns_none_on_error(fake_pool, monkeypatch):
    async def _fake(**_kw):
        return None, "error"

    monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)
    out = await llm_adapter.generate_summary("def f(): pass", "python", "a.py")
    assert out is None


# ---------------------------------------------------------------------------
# generate_hyde: resolves prompt_pins.effective("hyde"); the cache key
# starts with the version, so a pin change makes a second LLM call
# ---------------------------------------------------------------------------

async def test_generate_hyde_cache_key_starts_with_the_version(monkeypatch, registry_hyde_v2):
    monkeypatch.setattr(llm_adapter, "_HYDE_CACHE", {})
    _pin(monkeypatch, 2)

    async def _fake(**_kw):
        return "some code", "simple"

    monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)
    await llm_adapter.generate_hyde("find the parser", language="python")
    assert list(llm_adapter._HYDE_CACHE.keys()) == ["2|python|find the parser"]


async def test_generate_hyde_two_pins_make_two_llm_calls(monkeypatch, registry_hyde_v2):
    monkeypatch.setattr(llm_adapter, "_HYDE_CACHE", {})
    calls = []

    async def _fake(**_kw):
        calls.append(1)
        return "some code", "simple"

    monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)

    pin = {"v": 1}
    monkeypatch.setattr(prompt_pins, "effective", lambda op, source_id=None: pin["v"])

    out1 = await llm_adapter.generate_hyde("find the parser")
    assert len(calls) == 1

    # Same query, same pin: a cache hit, no second call.
    out2 = await llm_adapter.generate_hyde("find the parser")
    assert out2 == out1
    assert len(calls) == 1

    # Same query, a different HyDE pin: a distinct cache key, a new call.
    pin["v"] = 2
    out3 = await llm_adapter.generate_hyde("find the parser")
    assert out3 == out1  # the fake always returns the same text
    assert len(calls) == 2
