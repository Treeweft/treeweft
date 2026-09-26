"""LLM_TIMEOUT must not count time spent waiting for an LLM_CONCURRENCY slot.

call_with_control_layer wrapped `_chat` in `asyncio.wait_for(..., timeout)`,
and `_chat` acquires the LLM_CONCURRENCY semaphore inside it. An index job
fans out every uncached chunk summary at once across concurrent files, so
requests queued behind the semaphore timed out after LLM_TIMEOUT of *waiting*
while the LLM was healthy (~2.5s/call against a 30s timeout): 90 of 356
first attempts in a live run, which also tripped the circuit breaker.

Chunk summaries (background indexing) now bound only the request itself.
HyDE keeps the total deadline: its timeout is a query-latency budget, and a
search must not queue behind an indexing job's summaries.
"""
import asyncio

import pytest

from unittest.mock import MagicMock

from treeweft.adapters.llm_api import llm_adapter, llm_caller
from treeweft.domain.circuit_breaker import CircuitBreaker
from treeweft.domain.retry_engine import RetryConfig, RetryEngine

REQUEST_SECONDS = 0.2
TIMEOUT = 0.3  # > one request, < queue wait + one request


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "Defines f and returns its result."}}]}


class _SlowClient:
    async def post(self, url, json=None):
        await asyncio.sleep(REQUEST_SECONDS)
        return _Resp()


@pytest.fixture
def one_slot(monkeypatch):
    """LLM_CONCURRENCY=1, a slow-but-healthy LLM, one attempt, no audit/breaker state."""
    monkeypatch.setattr(llm_adapter, "_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(llm_adapter, "_get_client", lambda: _SlowClient())
    monkeypatch.setattr(llm_caller, "_breaker", CircuitBreaker())
    monkeypatch.setattr(llm_caller, "_audit", MagicMock())
    monkeypatch.setattr(llm_caller, "_retry", RetryEngine(RetryConfig(max_attempts=1)))


def _call(**kw):
    from treeweft.domain.audit import Operation

    return llm_caller.call_with_control_layer(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        max_tokens=60,
        operation=Operation.CHUNK_SUMMARY,
        timeout=TIMEOUT,
        **kw,
    )


@pytest.mark.asyncio
async def test_queued_request_does_not_time_out_while_waiting_for_a_slot(one_slot):
    results = await asyncio.gather(
        _call(timeout_includes_queue=False), _call(timeout_includes_queue=False)
    )
    assert [out for out, _ in results] == ["Defines f and returns its result."] * 2


@pytest.mark.asyncio
async def test_default_keeps_total_deadline_for_latency_bound_callers(one_slot):
    """HyDE semantics: the second caller's queue wait counts, so it gives up."""
    results = await asyncio.gather(_call(), _call())
    outs = sorted((out is not None) for out, _ in results)
    assert outs == [False, True]


@pytest.mark.asyncio
async def test_request_timeout_still_bounds_a_hung_request(monkeypatch):
    class _Hung:
        async def post(self, url, json=None):
            await asyncio.sleep(5)

    monkeypatch.setattr(llm_adapter, "_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(llm_adapter, "_get_client", lambda: _Hung())
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await llm_adapter._chat([], 10, request_timeout=0.1) is None
    assert loop.time() - t0 < 1.0


@pytest.mark.asyncio
async def test_chunk_summaries_opt_out_of_counting_queue_wait(monkeypatch):
    seen = {}

    async def _fake(**kw):
        seen.update(kw)
        return "Defines f.", "simple"

    monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)
    await llm_adapter._generate_summary("def f(): pass", "python", "a.py", version=3)
    assert seen.get("timeout_includes_queue") is False


@pytest.mark.asyncio
async def test_hyde_keeps_counting_queue_wait(monkeypatch):
    seen = {}

    async def _fake(**kw):
        seen.update(kw)
        return "def f(): pass", "simple"

    monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)
    await llm_adapter.generate_hyde("find the parser")
    assert seen.get("timeout_includes_queue", True) is True
