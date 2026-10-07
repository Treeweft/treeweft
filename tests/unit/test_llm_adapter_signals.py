"""Adapter tests for `_chat` — what it records about each response.

The endpoint is a mocked httpx client; spans are captured by an in-memory
exporter on a local TracerProvider, so nothing leaves the process.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from treeweft.adapters.llm_api import llm_adapter
from treeweft.domain.llm_response import ServedModelBaseline
from treeweft.infrastructure import metrics, tracing

PROMPT = "PROMPT-TEXT-THAT-MUST-NOT-BE-RECORDED"
ANSWER = "ANSWER-TEXT-THAT-MUST-NOT-BE-RECORDED"
MESSAGES = [{"role": "user", "content": PROMPT}]
OP = "chunk_summary"


def _body(**overrides) -> dict:
    body = {
        "model": "served-model-1",
        "choices": [{"message": {"content": ANSWER}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }
    body.update(overrides)
    return body


def _choice(content, finish_reason="stop") -> list[dict]:
    return [{"message": {"content": content}, "finish_reason": finish_reason}]


@pytest.fixture
def endpoint(mocker):
    """The mocked LLM endpoint; set `.body` to choose what it returns."""
    client = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = _body()
    client.post.return_value = response
    mocker.patch.object(llm_adapter, "_get_client", return_value=client)

    class Endpoint:
        def __init__(self):
            self.client = client

        @property
        def body(self):
            return response.json.return_value

        @body.setter
        def body(self, value):
            response.json.return_value = value

    return Endpoint()


@pytest.fixture(autouse=True)
def fresh_baseline(mocker):
    """Each test starts with no served model seen yet."""
    mocker.patch.object(llm_adapter, "_served_baseline", ServedModelBaseline())


@pytest.fixture
def spans(mocker):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    mocker.patch.object(tracing, "get_tracer", side_effect=provider.get_tracer)
    return exporter


@pytest.fixture
def no_tracing(mocker):
    mocker.patch.object(
        tracing, "get_tracer", side_effect=lambda *a, **kw: tracing._NoopTracer()
    )


def _only_span(exporter):
    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    return finished[0]


def _tokens(direction: str, operation: str = OP) -> float:
    return metrics.registry.get_sample_value(
        "treeweft_llm_tokens_total",
        {"operation": operation, "direction": direction},
    )


def _conditions(operation: str = OP) -> dict[str, float]:
    return {
        c: metrics.registry.get_sample_value(
            "treeweft_llm_response_conditions_total",
            {"operation": operation, "condition": c},
        )
        for c in ("model_mismatch", "model_changed", "truncated", "empty")
    }


def _flags(span) -> dict:
    return {
        k.removeprefix("treeweft.llm."): v
        for k, v in span.attributes.items()
        if k.startswith("treeweft.llm.")
    }


# ── User Story 1: what each call consumed and who served it ─────────────────

@pytest.mark.asyncio
async def test_span_carries_standard_attributes(endpoint, spans):
    result = await llm_adapter._chat(MESSAGES, 60, operation=OP)

    assert result == ANSWER
    span = _only_span(spans)
    assert span.name == "llm.chat"
    attrs = span.attributes
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == llm_adapter.LLM_MODEL
    assert attrs["gen_ai.request.max_tokens"] == 60
    assert attrs["gen_ai.response.model"] == "served-model-1"
    assert attrs["gen_ai.usage.input_tokens"] == 120
    assert attrs["gen_ai.usage.output_tokens"] == 30
    assert tuple(attrs["gen_ai.response.finish_reasons"]) == ("stop",)


@pytest.mark.asyncio
async def test_existing_attributes_are_unchanged(endpoint, spans):
    await llm_adapter._chat(MESSAGES, 60, operation=OP)

    attrs = _only_span(spans).attributes
    assert attrs["treeweft.llm_model"] == llm_adapter.LLM_MODEL
    assert attrs["treeweft.operation"] == OP
    assert attrs["treeweft.max_tokens"] == 60


@pytest.mark.asyncio
async def test_operation_defaults_to_unknown(endpoint, spans):
    before = _tokens("input", "unknown")

    await llm_adapter._chat(MESSAGES, 60)

    assert _only_span(spans).attributes["treeweft.operation"] == "unknown"
    assert _tokens("input", "unknown") == before + 120


@pytest.mark.asyncio
async def test_unreported_values_are_left_unset(endpoint, spans):
    endpoint.body = {"choices": [{"message": {"content": ANSWER}}]}
    before = _tokens("input"), _tokens("output")

    result = await llm_adapter._chat(MESSAGES, 60, operation=OP)

    assert result == ANSWER
    attrs = _only_span(spans).attributes
    for key in (
        "gen_ai.response.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.response.finish_reasons",
    ):
        assert key not in attrs
    assert (_tokens("input"), _tokens("output")) == before


@pytest.mark.asyncio
async def test_no_prompt_or_response_text_is_recorded(endpoint, spans):
    await llm_adapter._chat(MESSAGES, 60, operation=OP)

    span = _only_span(spans)
    recorded = repr(dict(span.attributes)) + repr(
        [(e.name, dict(e.attributes)) for e in span.events]
    )
    assert PROMPT not in recorded
    assert ANSWER not in recorded


@pytest.mark.asyncio
async def test_token_counter_rises_by_reported_counts(endpoint, spans):
    before = _tokens("input"), _tokens("output")

    await llm_adapter._chat(MESSAGES, 60, operation=OP)

    assert _tokens("input") == before[0] + 120
    assert _tokens("output") == before[1] + 30


def test_token_series_exist_from_import_for_every_operation():
    from treeweft.domain.audit import Operation

    for op in [o.value for o in Operation] + ["unknown"]:
        for direction in ("input", "output"):
            assert _tokens(direction, op) is not None


@pytest.mark.asyncio
async def test_malformed_usage_does_not_break_the_call(endpoint, spans):
    endpoint.body = _body(usage="lots")

    result = await llm_adapter._chat(MESSAGES, 60, operation=OP)

    assert result == ANSWER
    attrs = _only_span(spans).attributes
    assert "gen_ai.usage.input_tokens" not in attrs
    assert attrs["gen_ai.response.model"] == "served-model-1"


@pytest.mark.asyncio
async def test_failed_request_records_no_response_attributes(endpoint, spans):
    endpoint.client.post.side_effect = RuntimeError("connection refused")

    result = await llm_adapter._chat(MESSAGES, 60, operation=OP)

    assert result is None
    span = _only_span(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["gen_ai.request.model"] == llm_adapter.LLM_MODEL
    assert not [
        k for k in span.attributes
        if k.startswith(("gen_ai.response.", "gen_ai.usage.", "treeweft.llm."))
    ]


@pytest.mark.asyncio
async def test_one_request_per_call_and_same_result_without_tracing(
    endpoint, no_tracing
):
    before = _tokens("input"), _tokens("output")

    result = await llm_adapter._chat(MESSAGES, 60, operation=OP)

    assert result == ANSWER
    assert endpoint.client.post.call_count == 1
    # Tracing off: the counters still work.
    assert _tokens("input") == before[0] + 120
    assert _tokens("output") == before[1] + 30


# ── User Story 2: drift and degenerate responses are flagged ────────────────

@pytest.mark.asyncio
async def test_ordinary_response_is_checked_and_clean(endpoint, spans):
    before = _conditions()

    await llm_adapter._chat(MESSAGES, 60, operation=OP)

    assert _flags(_only_span(spans)) == {
        "model_mismatch": False, "model_changed": False,
        "truncated": False, "empty": False,
    }
    assert _conditions() == before


@pytest.mark.asyncio
async def test_served_model_change_is_flagged(endpoint, spans):
    before = _conditions()

    endpoint.body = _body(model="m-1")
    first = await llm_adapter._chat(MESSAGES, 60, operation=OP)
    endpoint.body = _body(model="m-2")
    second = await llm_adapter._chat(MESSAGES, 60, operation=OP)

    assert first == second == ANSWER
    one, two = spans.get_finished_spans()
    assert _flags(one)["model_mismatch"] is False
    assert _flags(two)["model_mismatch"] is True
    after = _conditions()
    assert after["model_mismatch"] == before["model_mismatch"] + 1
    assert after["truncated"] == before["truncated"]
    assert after["empty"] == before["empty"]


@pytest.mark.asyncio
async def test_one_swap_counts_one_change_but_a_mismatch_on_every_later_call(
    endpoint, spans
):
    before = _conditions()

    endpoint.body = _body(model="m-1")
    await llm_adapter._chat(MESSAGES, 60, operation=OP)
    endpoint.body = _body(model="m-2")
    for _ in range(3):
        await llm_adapter._chat(MESSAGES, 60, operation=OP)

    flags = [_flags(s) for s in spans.get_finished_spans()]
    assert [f["model_changed"] for f in flags] == [False, True, False, False]
    assert [f["model_mismatch"] for f in flags] == [False, True, True, True]
    after = _conditions()
    assert after["model_changed"] == before["model_changed"] + 1
    assert after["model_mismatch"] == before["model_mismatch"] + 3


@pytest.mark.asyncio
async def test_consistent_unrelated_served_name_is_never_flagged(endpoint, spans):
    before = _conditions()
    endpoint.body = _body(model="/models/qwen3-coder-q5_k_m.gguf")

    for _ in range(3):
        await llm_adapter._chat(MESSAGES, 60, operation=OP)

    assert [_flags(s)["model_mismatch"] for s in spans.get_finished_spans()] == [
        False, False, False,
    ]
    assert _conditions() == before


@pytest.mark.asyncio
async def test_truncated_response_is_flagged(endpoint, spans):
    before = _conditions()
    endpoint.body = _body(choices=_choice(ANSWER, "length"))

    result = await llm_adapter._chat(MESSAGES, 60, operation=OP)

    assert result == ANSWER
    assert _flags(_only_span(spans))["truncated"] is True
    assert _conditions()["truncated"] == before["truncated"] + 1


@pytest.mark.asyncio
async def test_empty_after_thinking_is_flagged(endpoint, spans):
    before = _conditions()
    endpoint.body = _body(choices=_choice("<think>hmm</think>"))

    result = await llm_adapter._chat(MESSAGES, 60, operation=OP)

    # Unchanged: the caller still gets the stripped (empty) text.
    assert result == ""
    assert _flags(_only_span(spans))["empty"] is True
    assert _conditions()["empty"] == before["empty"] + 1


@pytest.mark.asyncio
async def test_null_content_still_returns_none_and_is_flagged_empty(endpoint, spans):
    before = _conditions()
    endpoint.body = _body(choices=_choice(None))

    result = await llm_adapter._chat(MESSAGES, 60, operation=OP)

    # Unchanged: null content has always surfaced as None with an error span.
    assert result is None
    span = _only_span(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert _flags(span)["empty"] is True
    assert _conditions()["empty"] == before["empty"] + 1


@pytest.mark.asyncio
async def test_condition_is_absent_when_it_cannot_be_evaluated(endpoint, spans):
    before = _conditions()
    endpoint.body = {"choices": [{"message": {"content": ANSWER}}]}

    await llm_adapter._chat(MESSAGES, 60, operation=OP)

    # No served model and no finish reason reported: only `empty` is knowable.
    assert _flags(_only_span(spans)) == {"empty": False}
    assert _conditions() == before


@pytest.mark.asyncio
async def test_two_conditions_on_one_response_are_both_recorded(endpoint, spans):
    before = _conditions()
    endpoint.body = _body(model="m-1")
    await llm_adapter._chat(MESSAGES, 60, operation=OP)

    endpoint.body = _body(model="m-2", choices=_choice(ANSWER, "length"))
    await llm_adapter._chat(MESSAGES, 60, operation=OP)

    flags = _flags(spans.get_finished_spans()[1])
    assert flags == {"model_mismatch": True, "model_changed": True,
                     "truncated": True, "empty": False}
    after = _conditions()
    assert after["model_mismatch"] == before["model_mismatch"] + 1
    assert after["truncated"] == before["truncated"] + 1


@pytest.mark.asyncio
async def test_failed_request_records_no_conditions(endpoint, spans):
    before = _conditions()
    endpoint.client.post.side_effect = RuntimeError("connection refused")

    assert await llm_adapter._chat(MESSAGES, 60, operation=OP) is None

    assert _flags(_only_span(spans)) == {}
    assert _conditions() == before


@pytest.mark.asyncio
async def test_conditions_are_counted_without_tracing(endpoint, no_tracing):
    before = _conditions()
    endpoint.body = _body(choices=_choice(ANSWER, "length"))

    assert await llm_adapter._chat(MESSAGES, 60, operation=OP) == ANSWER

    assert _conditions()["truncated"] == before["truncated"] + 1


def test_condition_series_exist_from_import_for_every_operation():
    from treeweft.domain.audit import Operation

    for op in [o.value for o in Operation] + ["unknown"]:
        assert None not in _conditions(op).values()
