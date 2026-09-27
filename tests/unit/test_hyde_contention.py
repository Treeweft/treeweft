"""A search's HyDE call during an index job (#22).

An index job queues every uncached chunk summary behind LLM_CONCURRENCY.
HyDE used to wait behind all of them, time out while still queued, and be
retried three times: a search took about 18 s and still got no expansion.

- HyDE takes the next free slot ahead of queued summaries.
- LLM_HYDE_TIMEOUT is the budget for the whole HyDE call, retries
  included, so a timeout is never retried.
- HyDE and chunk summaries have separate circuit breakers, and a call
  that never reached the LLM does not count against either.
- A search that gets no expansion is counted.

The LLM is a fake client whose requests take a fixed time; slots, retries
and breakers are the real ones. Time limits in the assertions sit well
away from the expected value (several times a request or a backoff), so a
slow test host does not decide the result.
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
        assert seconds < 0.5
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

        (out, strategy), seconds = await _elapsed(_hyde(budget=0.3))

        assert (out, strategy) == (None, "error")
        assert layer.attempts(Operation.HYDE) == [FailureMode.TIMEOUT]
        assert client.requests == 1
        assert seconds < 0.6  # one budget; three and the backoff between is over 0.9

    async def test_timeout_while_queued_is_not_retried(self, layer):
        """The issue's case: every slot is busy for longer than the budget."""
        client = layer.client("ok", seconds=2.0)
        busy = asyncio.create_task(_summary(timeout=5.0))
        await asyncio.sleep(0.01)

        (out, strategy), seconds = await _elapsed(_hyde(budget=0.3))

        assert (out, strategy) == (None, "error")
        assert layer.attempts(Operation.HYDE) == [FailureMode.TIMEOUT]
        assert client.requests == 1  # the summary's; HyDE's was never sent
        assert seconds < 0.6
        busy.cancel()
        await asyncio.gather(busy, return_exceptions=True)

    async def test_fast_failure_is_retried_within_the_budget(self, layer):
        client = layer.client("fail", "ok")

        out, strategy = await _hyde(budget=1.0)

        assert out == "def parse(): return 1"
        assert strategy == "prompt_mutation"
        assert client.requests == 2

    async def test_no_retry_that_would_leave_less_than_half_the_budget(
        self, layer, monkeypatch
    ):
        """A request started with little of the budget left is sent only to
        be abandoned: the search waits the full budget for nothing."""
        monkeypatch.setattr(
            llm_caller,
            "_retry",
            RetryEngine(RetryConfig(max_attempts=3, base_delay_seconds=0.3, jitter_factor=0.0)),
        )
        client = layer.client("fail")

        # Backoff is 0.3 s, then 0.6 s. After the first, 0.7 s of the budget
        # is left: retry. After the second, 0.1 s would be: give up at 0.3 s.
        (out, strategy), seconds = await _elapsed(_hyde(budget=1.0))

        assert (out, strategy) == (None, "error")
        assert client.requests == 2
        assert 0.25 < seconds < 0.7

    async def test_budget_covers_a_retry_after_a_rejected_answer(self, layer):
        """An answer that fails validation is retried; the retry gets what
        is left of the budget, not a fresh one."""
        from treeweft.domain.response_validator import ValidationResult

        client = layer.client("ok", "hang")
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
                timeout=0.6,
                priority=Priority.INTERACTIVE,
            )
        )

        assert result[0] is None
        assert client.requests == 2
        assert seconds < 1.0  # a fresh budget for the retry would end at 1.2 or later

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
        assert layer.attempts(Operation.HYDE)[-1] == FailureMode.CIRCUIT_OPEN
        assert seconds < 0.2

    async def test_running_out_of_budget_in_the_queue_is_not_a_provider_failure(self, layer):
        """Every slot holds a slow chunk summary. Searches give up in the
        queue, the LLM is never asked, and once a slot frees HyDE must work
        at once, not sit behind an open breaker for 30 s."""
        layer.client("ok", seconds=0.5)
        busy = asyncio.create_task(_summary(timeout=5.0))
        await asyncio.sleep(0.01)
        for _ in range(6):  # past the threshold of 5
            assert await _hyde(budget=0.02) == (None, "error")
        await busy

        layer.client("ok")
        out, _ = await _hyde(budget=1.0)

        assert out == "def parse(): return 1"
        assert FailureMode.CIRCUIT_OPEN not in layer.attempts(Operation.HYDE)

    async def test_cancelled_probe_does_not_leave_the_breaker_shut(self, layer, monkeypatch):
        """HALF_OPEN admits one probe. If that call is cancelled, the probe
        must be given back, or HyDE stays off until the indexer restarts."""
        from treeweft.domain.circuit_breaker import CircuitBreaker, CircuitConfig

        breaker = CircuitBreaker(CircuitConfig(failure_threshold=1, recovery_seconds=0.05))
        breaker.record_failure()
        monkeypatch.setattr(llm_caller, "_breakers", {Operation.HYDE.value: breaker})
        await asyncio.sleep(0.08)  # OPEN -> HALF_OPEN

        layer.client("hang")
        probe = asyncio.create_task(_hyde(budget=5.0))
        await asyncio.sleep(0.02)
        probe.cancel()
        await asyncio.gather(probe, return_exceptions=True)

        layer.client("ok")
        out, _ = await _hyde(budget=1.0)

        assert out == "def parse(): return 1"

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
        monkeypatch.setenv("LLM_SEARCH_RESERVED_SLOTS", "0")  # tested in test_llm_search_reserved_slots.py

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

    async def test_rejected_answers_are_counted_as_rejected(self, monkeypatch):
        async def _fake(**_kw):
            return None, "rejected"

        monkeypatch.setattr(llm_caller, "call_with_control_layer", _fake)
        monkeypatch.setattr(llm_adapter, "_HYDE_CACHE", {})
        before = self._count("rejected")

        assert await llm_adapter.generate_hyde("find the parser") is None

        assert self._count("rejected") == before + 1

    async def test_expansion_that_cannot_be_embedded_is_counted(self, monkeypatch):
        from treeweft.application import retrieval

        async def _hyde_text(query, language=None):
            return "def parse(): return 1"

        async def _no_embedding(texts):
            return []

        monkeypatch.setattr(retrieval.llm, "generate_hyde", _hyde_text)
        monkeypatch.setattr(retrieval.embedder, "embed", _no_embedding)
        before = self._count("embed_failed")

        assert await retrieval._maybe_hyde_embedding("find the parser", None, True) is None

        assert self._count("embed_failed") == before + 1

    async def test_every_reason_is_exported_before_the_first_fallback(self):
        """rate() and increase() miss the first increment of a series that
        did not exist before it."""
        exported = {
            sample.labels["reason"]
            for metric in metrics.hyde_fallbacks.collect()
            for sample in metric.samples
            if sample.name == "treeweft_hyde_fallbacks_total"
        }

        assert exported >= {"error", "rejected", "embed_failed"}
