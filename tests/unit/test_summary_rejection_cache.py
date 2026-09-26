"""Chunk summaries the validator rejects must not be regenerated on every run.

`_SUMMARY_SCHEMA` forbids phrases such as "this code", but the summary prompt
never said so. llama3.1:8b opens nearly every summary with "This code ...", so
each chunk burned three full LLM calls (plus backoff) and, because only
successful summaries were cached, paid that cost again on every re-index —
an 84-file repo took 313s with 524 LLM calls for ~200 cache misses.

Two fixes, guarded here:
  * the prompt names the openers the validator rejects;
  * a summary rejected on every attempt is cached as a negative marker ("")
    so the chunk is not re-summarized, while timeouts / LLM errors are NOT
    cached (they are transient and must be retried next run). The marker is
    visible only to the indexing path; other cache readers (summary_tail)
    never see an empty summary.
"""
from unittest.mock import MagicMock

import pytest

from treeweft.adapters.llm_api import llm_adapter, llm_caller, prompts
from treeweft.domain.circuit_breaker import CircuitBreaker

_SUMMARY_PV = prompts.get("chunk_summary", 3)


# ---------------------------------------------------------------------------
# Fix 1: the prompt states what the validator rejects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", ["This code", "Here is"])
def test_summary_prompt_names_rejected_openers(phrase):
    assert phrase.lower() in _SUMMARY_PV.schema.forbidden_phrases
    assert f'"{phrase}"' in _SUMMARY_PV.system  # quoted, as an explicit ban


def test_summary_prompt_does_not_itself_use_a_forbidden_phrase():
    """The old prompt asked for a sentence 'describing what this code does',
    priming the model with the exact phrase the validator rejects."""
    prompt = _SUMMARY_PV.system.lower()
    for phrase in _SUMMARY_PV.schema.forbidden_phrases:
        # allow only the explicit quoted ban itself
        assert prompt.replace(f'"{phrase.lower()}"', "").count(phrase.lower()) == 0, phrase


# ---------------------------------------------------------------------------
# call_with_control_layer: rejection is distinguishable from failure
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_caller(monkeypatch):
    """Fresh breaker, silent audit, no retry backoff."""
    monkeypatch.setattr(llm_caller, "_breaker", CircuitBreaker())
    monkeypatch.setattr(llm_caller, "_audit", MagicMock())

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(llm_caller.asyncio, "sleep", _no_sleep)


def _summary_call():
    from treeweft.domain.audit import Operation
    from treeweft.domain.response_validator import ResponseValidator

    return llm_caller.call_with_control_layer(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        max_tokens=60,
        operation=Operation.CHUNK_SUMMARY,
        validator=ResponseValidator(_SUMMARY_PV.response_schema()),
    )


@pytest.mark.asyncio
async def test_every_attempt_rejected_reports_rejected(isolated_caller, monkeypatch):
    calls = []

    async def _chat(messages, max_tokens, *, operation=""):
        calls.append(1)
        return "This code defines a thing."

    monkeypatch.setattr(llm_caller, "_chat", _chat)
    out, strategy = await _summary_call()
    assert out is None
    assert strategy == "rejected"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_llm_failure_still_reports_error(isolated_caller, monkeypatch):
    async def _chat(messages, max_tokens, *, operation=""):
        return None

    monkeypatch.setattr(llm_caller, "_chat", _chat)
    out, strategy = await _summary_call()
    assert (out, strategy) == (None, "error")


# ---------------------------------------------------------------------------
# summarize_with_cache: negative-cache rejections only
# ---------------------------------------------------------------------------

class _FakeCache:
    def __init__(self):
        self.rows: dict[str, str] = {}

    async def get_many(self, sha1s, prompt_version, include_rejected=False):
        return {
            k: v for k, v in self.rows.items()
            if k in sha1s and (v or include_rejected)
        }

    async def put(self, sha1, summary, *, prompt_version):
        self.rows[sha1] = summary


@pytest.fixture
def fake_cache(monkeypatch):
    c = _FakeCache()
    monkeypatch.setattr(llm_adapter, "cache_get_many", c.get_many)
    monkeypatch.setattr(llm_adapter, "cache_put", c.put)
    return c


def _stub_generation(monkeypatch, result):
    calls = []

    async def _gen(chunk_text, language, file_path, *, version):
        calls.append(chunk_text)
        return result

    monkeypatch.setattr(llm_adapter, "_generate_summary", _gen)
    return calls


@pytest.mark.asyncio
async def test_rejected_summary_is_negative_cached_and_not_regenerated(fake_cache, monkeypatch):
    calls = _stub_generation(monkeypatch, (None, "rejected"))
    out = await llm_adapter.summarize_with_cache("def f(): pass", "python", "a.py", version=3)
    assert out == (None, "rejected")
    assert fake_cache.rows == {llm_adapter.chunk_cache_key("def f(): pass"): ""}
    out2 = await llm_adapter.summarize_with_cache("def f(): pass", "python", "a.py", version=3)
    assert out2 == (None, "cached")  # served by the negative marker
    assert len(calls) == 1  # second call served by the negative marker, not regenerated


@pytest.mark.asyncio
async def test_llm_error_is_not_cached(fake_cache, monkeypatch):
    calls = _stub_generation(monkeypatch, (None, "error"))
    assert await llm_adapter.summarize_with_cache(
        "def f(): pass", "python", "a.py", version=3) == (None, "error")
    assert await llm_adapter.summarize_with_cache(
        "def f(): pass", "python", "a.py", version=3) == (None, "error")
    assert fake_cache.rows == {}
    assert len(calls) == 2  # transient failure: retried next time


@pytest.mark.asyncio
async def test_successful_summary_is_cached(fake_cache, monkeypatch):
    _stub_generation(monkeypatch, ("Defines f.", "simple"))
    out = await llm_adapter.summarize_with_cache("def f(): pass", "python", "a.py", version=3)
    assert out == ("Defines f.", "generated")
    assert list(fake_cache.rows.values()) == ["Defines f."]


# ---------------------------------------------------------------------------
# cache_get_many: the marker is hidden from ordinary readers
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_args):
        return self._rows


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return _FakeConn(pool._rows)

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_cache_get_many_hides_rejection_markers_by_default(monkeypatch):
    rows = [{"sha1": "a", "summary": "Defines a."}, {"sha1": "b", "summary": ""}]

    async def _get_pool():
        return _FakePool(rows)

    monkeypatch.setattr(llm_adapter, "get_pool", _get_pool)
    assert await llm_adapter.cache_get_many(["a", "b"], 3) == {"a": "Defines a."}
    assert await llm_adapter.cache_get_many(["a", "b"], 3, include_rejected=True) == {
        "a": "Defines a.", "b": "",
    }


# ---------------------------------------------------------------------------
# Indexing path: a marker counts as cached (no summary vector, no LLM call)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summaries_for_chunks_treats_marker_as_cached_none(monkeypatch):
    from treeweft.application import indexer_runners

    llm = indexer_runners.llm  # patch the object the runner holds (other tests swap modules)

    chunks = [
        {"text": "def a(): pass", "file_path": "a.py", "language": "python"},
        {"text": "def b(): pass", "file_path": "b.py", "language": "python"},
    ]
    ka, kb = (llm.chunk_cache_key(c["text"]) for c in chunks)

    async def _get_many(sha1s, prompt_version=None, include_rejected=False):
        rows = {ka: "Defines a.", kb: ""}
        return {k: v for k, v in rows.items() if v or include_rejected}

    generated = []

    async def _summarize(*args, **kwargs):
        generated.append((args, kwargs))
        return "should not be called", "generated"

    monkeypatch.setattr(llm, "cache_get_many", _get_many)
    monkeypatch.setattr(llm, "summarize_with_cache", _summarize)
    outcomes = await indexer_runners._summaries_for_chunks(chunks, version=3)
    assert outcomes == [("Defines a.", "cached"), (None, "rejected")]
    assert generated == []
