"""A search's HyDE call during an index job (#22).

An index job queues every uncached chunk summary behind LLM_CONCURRENCY.
HyDE used to wait behind all of them, time out while still queued, and be
retried three times: a search took about 18 s and still got no expansion.

- HyDE takes the next free slot ahead of queued summaries.
- LLM_HYDE_TIMEOUT is the budget for the whole HyDE call, retries
  included, so a timeout is never retried.
- HyDE and chunk summaries have separate circuit breakers.
- A search that gets no expansion is counted.

The LLM is a fake client whose requests take a fixed time; slots, retries
and breakers are the real ones.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from treeweft.adapters.llm_api import llm_adapter, llm_caller
from treeweft.domain.audit import FailureMode, Operation
from treeweft.domain.priority_slots import Priority, PrioritySlots
from treeweft.domain.retry_engine import RetryConfig, RetryEngine
from treeweft.infrastructure import metrics

pytestmark = pytest.mark.asyncio

REQUEST_SECONDS = 0.05
MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "def parse(): return 1"}}]}


class _Client:
    """`behaviors` are consumed one per request; the last one repeats."""

    def __init__(self, *behaviors: str, seconds: float = REQUEST_SECONDS):
        self._behaviors = list(behaviors) or ["ok"]
        self._seconds = seconds
        self.requests = 0

    async def post(self, url, json=None):
        self.requests += 1
        behavior = self._behaviors.pop(0) if len(self._behaviors) > 1 else self._behaviors[0]
        if behavior == "hang":
            await asyncio.sleep(60)
        if behavior == "fail":
            raise RuntimeError("503 from the provider")
        await asyncio.sleep(self._seconds)
        return _Resp()


class _Layer:
    def __init__(self, monkeypatch):
        self._monkeypatch = monkeypatch
        self.audit = MagicMock()

    def client(self, *behaviors: str, seconds: float = REQUEST_SECONDS) -> _Client:
        client = _Client(*behaviors, seconds=seconds)
        self._monkeypatch.setattr(llm_adapter, "_get_client", lambda: client)
        return client

    def attempts(self, operation: Operation) -> list[FailureMode]:
        return [
            c.kwargs["failure_mode"]
            for c in self.audit.log.call_args_list
            if c.kwargs["operation"] == operation
        ]


@pytest.fixture
def layer(monkeypatch):
    """One LLM slot, three attempts with a short fixed backoff, fresh breakers."""
    monkeypatch.setattr(llm_adapter, "_slots", PrioritySlots(1))
    monkeypatch.setattr(llm_caller, "_breakers", {})
    monkeypatch.setattr(
        llm_caller,
        "_retry",
        RetryEngine(RetryConfig(max_attempts=3, base_delay_seconds=0.02, jitter_factor=0.0)),
    )
    made = _Layer(monkeypatch)
    monkeypatch.setattr(llm_caller, "_audit", made.audit)
    return made


def _hyde(budget: float):
    return llm_caller.call_with_control_layer(
        messages=[dict(m) for m in MESSAGES],
        max_tokens=256,
        operation=Operation.HYDE,
        timeout=budget,
        priority=Priority.INTERACTIVE,
    )


def _summary(timeout: float = 1.0):
    return llm_caller.call_with_control_layer(
        messages=[dict(m) for m in MESSAGES],
        max_tokens=60,
        operation=Operation.CHUNK_SUMMARY,
        timeout=timeout,
        timeout_includes_queue=False,
        priority=Priority.BACKGROUND,
    )


async def _elapsed(awaitable):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    result = await awaitable
    return result, loop.time() - t0


class TestHydeDuringIndexing:
    async def test_hyde_takes_the_next_slot_ahead_of_queued_summaries(self, layer):
        client = layer.client("ok")
        summaries = [asyncio.create_task(_summary()) for _ in range(20)]
        await asyncio.sleep(REQUEST_SECONDS / 2)  # one in flight, 19 queued

        # Behind the queue it would wait 20 requests: a second, twice the budget.
        (out, strategy), seconds = await _elapsed(_hyde(budget=0.5))

        assert out == "def parse(): return 1"
        assert strategy == "simple"
        assert seconds < REQUEST_SECONDS * 4
        assert client.requests <= 3  # the one in flight, HyDE, at most one more started
        results = await asyncio.gather(*summaries)
        assert all(out is not None for out, _ in results)

    async def test_summaries_are_not_starved_by_a_stream_of_searches(self, layer):
        """Priority, not reservation: a summary still gets the slot whenever
        no search is waiting."""
        layer.client("ok")
        summary = asyncio.create_task(_summary())
        searches = [await _hyde(budget=0.5) for _ in range(3)]

        out, _ = await asyncio.wait_for(summary, timeout=1.0)

        assert out is not None
        assert all(out is not None for out, _ in searches)


class TestHydeBudget:
    async def test_timeout_is_not_retried(self, layer):
        client = layer.client("hang")

        (out, strategy), seconds = await _elapsed(_hyde(budget=0.2))

        assert (out, strategy) == (None, "error")
        assert layer.attempts(Operation.HYDE) == [FailureMode.TIMEOUT]
        assert client.requests == 1
        assert seconds < 0.3  # one budget, not three and the backoff between

    async def test_timeout_while_queued_is_not_retried(self, layer):
        """The issue's case: every slot is busy for longer than the budget."""
        layer.client("ok", seconds=1.0)
        busy = asyncio.create_task(_hyde(budget=5.0))
        await asyncio.sleep(0.01)

        (out, strategy), seconds = await _elapsed(_hyde(budget=0.2))

        assert (out, strategy) == (None, "error")
        assert seconds < 0.3
        busy.cancel()

    async def test_fast_failure_is_retried_within_the_budget(self, layer):
        client = layer.client("fail", "ok")

        out, strategy = await _hyde(budget=1.0)

        assert out == "def parse(): return 1"
        assert strategy == "prompt_mutation"
        assert client.requests == 2

    async def test_retries_stop_when_the_backoff_would_pass_the_budget(self, layer):
        client = layer.client("fail")

        # Backoff is 0.02 s then 0.04 s. A budget of 0.03 s has room for the
        # first and not for the second.
        (out, strategy), seconds = await _elapsed(_hyde(budget=0.03))

        assert (out, strategy) == (None, "error")
        assert client.requests == 2
        assert seconds < 0.1

    async def test_budget_covers_a_retry_after_a_rejected_answer(self, layer, monkeypatch):
        """An answer that fails validation is retried; the retry gets what
        is left of the budget, not a fresh one."""
        from treeweft.domain.response_validator import ValidationResult

        layer.client("ok", "hang")
        validator = MagicMock()
        validator.validate.return_value = ValidationResult(
            passed=False, failure_mode=FailureMode.SCHEMA_VIOLATION
        )

        result, seconds = await _elapsed(
            llm_caller.call_with_control_layer(
                messages=[dict(m) for m in MESSAGES],
                max_tokens=256,
                operation=Operation.HYDE,
                validator=validator,
                timeout=0.3,
                priority=Priority.INTERACTIVE,
            )
        )

        assert result[0] is None
        assert seconds < 0.4

    async def test_summaries_keep_every_attempt(self, layer):
        client = layer.client("fail")

        out, strategy = await _summary()

        assert (out, strategy) == (None, "error")
        assert client.requests == 3


class TestBreakers:
    async def test_hyde_failures_do_not_open_the_summary_breaker(self, layer):
        layer.client("fail")
        for _ in range(3):  # 3 calls x up to 3 attempts: past the threshold of 5
            await _hyde(budget=1.0)
        assert FailureMode.CIRCUIT_OPEN in layer.attempts(Operation.HYDE)

        layer.client("ok")
        out, strategy = await _summary()

        assert out == "def parse(): return 1"
        assert FailureMode.CIRCUIT_OPEN not in layer.attempts(Operation.CHUNK_SUMMARY)

    async def test_summary_failures_do_not_open_the_hyde_breaker(self, layer):
        layer.client("fail")
        for _ in range(3):
            await _summary()
        assert FailureMode.CIRCUIT_OPEN in layer.attempts(Operation.CHUNK_SUMMARY)

        layer.client("ok")
        out, _ = await _hyde(budget=1.0)

        assert out == "def parse(): return 1"

    async def test_open_hyde_breaker_answers_at_once(self, layer):
        client = layer.client("fail")
        for _ in range(3):
            await _hyde(budget=1.0)
        sent = client.requests

        (out, strategy), seconds = await _elapsed(_hyde(budget=1.0))

        assert (out, strategy) == (None, "error")
        assert client.requests == sent
        assert seconds < 0.02

    async def test_breaker_state_reports_the_worst_breaker(self, layer):
        assert llm_caller.get_breaker_state() == "CLOSED"
        layer.client("fail")
        for _ in range(3):
            await _hyde(budget=1.0)
        layer.client("ok")
        await _summary()

        assert llm_caller.get_breaker_state() == "OPEN"


class TestWiring:
    async def test_hyde_is_interactive_with_a_total_budget(self, monkeypatch):
        seen = {}

        async def _fake(**kw):
            seen.update(kw)
            return "def f(): pass", "simple"

        monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)
        monkeypatch.setattr(llm_adapter, "_HYDE_CACHE", {})

        await llm_adapter.generate_hyde("find the parser")

        assert seen["priority"] is Priority.INTERACTIVE
        assert seen.get("timeout_includes_queue", True) is True
        assert seen["timeout"] == llm_adapter.LLM_HYDE_TIMEOUT

    async def test_chunk_summaries_are_background(self, monkeypatch):
        seen = {}

        async def _fake(**kw):
            seen.update(kw)
            return "Defines f.", "simple"

        monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)

        await llm_adapter._generate_summary("def f(): pass", "python", "a.py", version=3)

        assert seen["priority"] is Priority.BACKGROUND

    async def test_slots_are_sized_by_llm_concurrency(self, monkeypatch):
        monkeypatch.setattr(llm_adapter, "_slots", None)
        monkeypatch.setattr(llm_adapter, "LLM_CONCURRENCY", 3)

        slots = llm_adapter._get_slots()

        for _ in range(3):
            await asyncio.wait_for(slots.acquire(Priority.BACKGROUND), timeout=0.1)
        assert slots.in_use == 3
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slots.acquire(Priority.INTERACTIVE), timeout=0.01)


class TestFallbackIsCounted:
    """Constitution V: a search that ran without HyDE must be observable."""

    @staticmethod
    def _count(reason: str) -> float:
        return metrics.hyde_fallbacks.labels(reason=reason)._value.get()

    async def test_search_without_an_expansion_is_counted(self, layer, monkeypatch):
        monkeypatch.setattr(llm_adapter, "_HYDE_CACHE", {})
        monkeypatch.setattr(llm_adapter, "LLM_HYDE_TIMEOUT", 0.1)
        layer.client("hang")
        before = self._count("error")

        out = await llm_adapter.generate_hyde("where is the retry budget enforced")

        assert out is None
        assert self._count("error") == before + 1

    async def test_successful_expansion_is_not_counted(self, layer, monkeypatch):
        monkeypatch.setattr(llm_adapter, "_HYDE_CACHE", {})
        layer.client("ok")
        before = self._count("error"), self._count("rejected")

        out = await llm_adapter.generate_hyde("where is the retry budget enforced")

        assert out
        assert (self._count("error"), self._count("rejected")) == before
