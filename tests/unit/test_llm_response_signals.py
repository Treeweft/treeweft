"""Domain tests for llm_response — what one chat response reports about itself."""
import dataclasses

import pytest

from treeweft.domain.llm_response import (
    ResponseSignals,
    ServedModelBaseline,
    is_truncation,
    strip_thinking,
)


def _body(**overrides) -> dict:
    body = {
        "model": "served-model-1",
        "choices": [
            {"message": {"content": "def f(): pass"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }
    body.update(overrides)
    return body


# ── ResponseSignals ─────────────────────────────────────────────────────────

def test_full_body_yields_every_field():
    s = ResponseSignals.from_response(_body())
    assert s.served_model == "served-model-1"
    assert s.input_tokens == 120
    assert s.output_tokens == 30
    assert s.finish_reason == "stop"
    assert s.truncated is False
    assert s.empty is False


@pytest.mark.parametrize("body", [
    {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]},
    _body(usage=None),
    _body(usage={}),
    _body(usage={"prompt_tokens": "120", "completion_tokens": 3.5}),
    _body(usage={"prompt_tokens": True, "completion_tokens": None}),
])
def test_missing_usage_is_none_never_zero(body):
    s = ResponseSignals.from_response(body)
    assert s.input_tokens is None
    assert s.output_tokens is None


@pytest.mark.parametrize("model", [None, "", 0])
def test_missing_served_model_is_none(model):
    s = ResponseSignals.from_response(_body(model=model))
    assert s.served_model is None


def test_absent_model_key_is_none():
    body = _body()
    del body["model"]
    assert ResponseSignals.from_response(body).served_model is None


def test_null_finish_reason_is_none_and_truncated_unknown():
    s = ResponseSignals.from_response(
        _body(choices=[{"message": {"content": "x"}, "finish_reason": None}])
    )
    assert s.finish_reason is None
    assert s.truncated is None


@pytest.mark.parametrize("reason,expected", [
    ("length", True),
    ("max_tokens", True),
    ("LENGTH", True),
    ("Max_Tokens", True),
    ("stop", False),
    ("tool_calls", False),
])
def test_truncated_follows_finish_reason(reason, expected):
    s = ResponseSignals.from_response(
        _body(choices=[{"message": {"content": "x"}, "finish_reason": reason}])
    )
    assert s.truncated is expected
    assert is_truncation(reason) is expected


def test_is_truncation_unknown_is_none():
    assert is_truncation(None) is None
    assert is_truncation("") is None


@pytest.mark.parametrize("message", [
    {},
    {"content": None},
    {"content": ""},
    {"content": "   \n"},
    {"content": "<think>working it out</think>"},
    {"content": "<THINK>working\nit out</THINK>\n  "},
])
def test_empty_when_no_usable_text(message):
    s = ResponseSignals.from_response(
        _body(choices=[{"message": message, "finish_reason": "stop"}])
    )
    assert s.empty is True


def test_not_empty_for_ordinary_text_after_thinking():
    s = ResponseSignals.from_response(
        _body(choices=[{"message": {"content": "<think>hm</think>answer"},
                        "finish_reason": "stop"}])
    )
    assert s.empty is False


@pytest.mark.parametrize("body", [{}, _body(choices=[]), _body(choices=None)])
def test_empty_unknown_without_choices(body):
    assert ResponseSignals.from_response(body).empty is None


@pytest.mark.parametrize("body", [
    {},
    None,
    [],
    "not a dict",
    42,
    _body(usage="lots"),
    _body(choices=[]),
    _body(choices=["a string"]),
    _body(choices=[{"message": "a string", "finish_reason": 7}]),
    _body(choices={"0": {}}),
])
def test_from_response_never_raises(body):
    s = ResponseSignals.from_response(body)
    assert isinstance(s, ResponseSignals)


def test_non_string_finish_reason_is_none():
    s = ResponseSignals.from_response(
        _body(choices=[{"message": {"content": "x"}, "finish_reason": 7}])
    )
    assert s.finish_reason is None
    assert s.truncated is None


def test_signals_hold_no_prompt_or_response_text():
    secret = "the response text must not be kept"
    s = ResponseSignals.from_response(
        _body(choices=[{"message": {"content": secret}, "finish_reason": "stop"}])
    )
    assert {f.name for f in dataclasses.fields(s)} == {
        "served_model", "input_tokens", "output_tokens",
        "finish_reason", "truncated", "empty",
    }
    assert secret not in repr(s)


def test_signals_are_immutable():
    s = ResponseSignals.from_response(_body())
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.served_model = "other"


# ── strip_thinking ──────────────────────────────────────────────────────────

def test_strip_thinking_removes_blocks_case_insensitively():
    assert strip_thinking("<think>a</think>b") == "b"
    assert strip_thinking("<THINK>a\nb</THINK>  c ") == "c"


def test_strip_thinking_treats_none_as_empty():
    assert strip_thinking(None) == ""


def test_agent_protocol_reexports_the_same_function():
    from treeweft.domain.benchmark import agent_protocol

    assert agent_protocol.strip_thinking is strip_thinking


# ── ServedModelBaseline ─────────────────────────────────────────────────────

def test_baseline_ignores_missing_served_model():
    b = ServedModelBaseline()
    assert b.observe("m", None) is None
    # Nothing was remembered: the next reported name sets the baseline.
    assert b.observe("m", "served-1") is False
    assert b.observe("m", "served-2") is True


def test_baseline_first_seen_then_same_then_different():
    b = ServedModelBaseline()
    assert b.observe("m", "served-1") is False
    assert b.observe("m", "served-1") is False
    assert b.observe("m", "served-2") is True


def test_baseline_never_moves():
    b = ServedModelBaseline()
    b.observe("m", "served-1")
    assert b.observe("m", "served-2") is True
    assert b.observe("m", "served-1") is False
    assert b.observe("m", "served-2") is True


def test_baselines_are_independent_per_requested_model():
    b = ServedModelBaseline()
    assert b.observe("a", "served-a") is False
    assert b.observe("b", "served-b") is False
    assert b.observe("a", "served-a") is False
    assert b.observe("b", "served-a") is True


@pytest.mark.parametrize("served", [
    "/models/qwen3-coder-q5_k_m.gguf",
    "vendor-model-2026-08-01",
])
def test_baseline_consistent_unrelated_form_is_never_a_mismatch(served):
    b = ServedModelBaseline()
    for _ in range(5):
        assert b.observe("configured-alias", served) is False
