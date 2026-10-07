"""The agent loop's per-run tool-call figures and truncation count, under both
protocols.

Detroit-style: the only fake is the out-of-process LLM boundary. One script
of (tool, args) steps is rendered as ReAct text or as native tool_calls, so
the same behaviour is asserted for both loops.
"""
from __future__ import annotations

import json

import pytest

from treeweft.adapters.benchmark.agent_llm import ChatResult
from treeweft.adapters.benchmark.tool_base import Tool
from treeweft.application.benchmark.agent_loop import run_agent

_USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

PROTOCOLS = pytest.mark.parametrize("native", [False, True], ids=["react", "native"])


def _react(step) -> str:
    if step == "FINAL":
        return "THOUGHT: done\nFINAL_ANSWER: the answer"
    tool, args = step
    return f"THOUGHT: look\nACTION: {tool}({json.dumps(args)})"


def _native(step, i: int):
    if step == "FINAL":
        return "the answer", None
    tool, args = step
    arguments = args if isinstance(args, str) else json.dumps(args)
    return "", [{"id": f"c{i}", "type": "function",
                 "function": {"name": tool, "arguments": arguments}}]


class _ChatOnlyLLM:
    """A client with only chat() — the shape every existing fake has."""

    def __init__(self, steps: list, native: bool):
        self.steps = list(steps)
        self.native = native
        self.llm_calls = 0

    def _reply(self):
        step = self.steps.pop(0)
        i = self.llm_calls
        self.llm_calls += 1
        if self.native:
            return _native(step, i)
        return _react(step), None

    async def chat(self, messages, *, tools=None, max_tokens=None):
        content, tcs = self._reply()
        return content, dict(_USAGE), tcs


class _FullLLM(_ChatOnlyLLM):
    """A client with chat_full(); `finish_reasons` is consumed one per call."""

    def __init__(self, steps: list, native: bool, finish_reasons: list):
        super().__init__(steps, native)
        self.finish_reasons = list(finish_reasons)

    async def chat_full(self, messages, *, tools=None, max_tokens=None):
        content, tcs = self._reply()
        return ChatResult(content=content, usage=dict(_USAGE), tool_calls=tcs,
                          finish_reason=self.finish_reasons.pop(0))

    async def chat(self, messages, *, tools=None, max_tokens=None):
        raise AssertionError("the loop must prefer chat_full when it exists")


def _tools(executed: list) -> list[Tool]:
    def make(name: str) -> Tool:
        async def run(**kw):
            executed.append((name, kw))
            return {"ok": name}
        return Tool(name=name, description=name, run=run)

    return [make("search_code"), make("read_file")]


async def _run(llm, executed: list, native: bool, **kw):
    return await run_agent(query="q", tools=_tools(executed), llm=llm,
                           retrieved_files=[], native_tools=native, **kw)


@PROTOCOLS
@pytest.mark.asyncio
async def test_three_identical_calls_and_two_different(native):
    steps = [
        ("search_code", {"query": "x"}),
        ("search_code", {"query": "x"}),
        ("search_code", {"query": "x"}),
        ("read_file", {"path": "a.py"}),
        ("read_file", {"path": "b.py"}),
        "FINAL",
    ]
    executed: list = []
    llm = _ChatOnlyLLM(steps, native)

    res = await _run(llm, executed, native)

    assert res.tool_call_counts == {"search_code": 3, "read_file": 2}
    assert res.repeated_tool_calls == 2
    assert res.looped is True
    # This client reports no finish reason: unknown, not "none were cut off".
    assert res.truncated_responses is None
    # Existing figures and the work done are what they always were.
    assert res.tool_calls == 5 == len(executed)
    assert sum(res.tool_call_counts.values()) == res.tool_calls
    assert res.turns == 6
    assert llm.llm_calls == 6
    assert res.token_account.total_tokens == 6 * _USAGE["total_tokens"]
    assert res.final_answer == "the answer"


@PROTOCOLS
@pytest.mark.asyncio
async def test_four_different_searches_are_counted_not_repeated(native):
    steps = [("search_code", {"query": f"q{i}"}) for i in range(4)] + ["FINAL"]

    res = await _run(_ChatOnlyLLM(steps, native), [], native)

    # read_file is exposed and was never called: zero, not missing.
    assert res.tool_call_counts == {"search_code": 4, "read_file": 0}
    assert res.repeated_tool_calls == 0
    assert res.looped is False


@PROTOCOLS
@pytest.mark.asyncio
async def test_reordered_argument_keys_are_the_same_call(native):
    steps = [
        ("search_code", {"query": "x", "top_k": 5}),
        ("search_code", {"top_k": 5, "query": "x"}),
        "FINAL",
    ]

    res = await _run(_ChatOnlyLLM(steps, native), [], native)

    assert res.repeated_tool_calls == 1
    assert res.looped is False


@PROTOCOLS
@pytest.mark.asyncio
async def test_unknown_tool_is_not_counted(native):
    steps = [
        ("no_such_tool", {"query": "x"}),
        ("no_such_tool", {"query": "x"}),
        ("no_such_tool", {"query": "x"}),
        ("search_code", {"query": "x"}),
        "FINAL",
    ]
    executed: list = []

    res = await _run(_ChatOnlyLLM(steps, native), executed, native)

    assert res.tool_call_counts == {"search_code": 1, "read_file": 0}
    assert res.repeated_tool_calls == 0
    assert res.looped is False
    assert res.tool_calls == 1 == len(executed)


@pytest.mark.asyncio
async def test_unparseable_arguments_are_not_counted_native():
    steps = [
        ("search_code", "{not json"),
        ("search_code", "{not json"),
        ("search_code", "{not json"),
        ("search_code", {"query": "x"}),
        "FINAL",
    ]
    executed: list = []

    res = await _run(_ChatOnlyLLM(steps, True), executed, True)

    assert res.tool_call_counts == {"search_code": 1, "read_file": 0}
    assert res.repeated_tool_calls == 0
    assert res.looped is False
    assert res.tool_calls == 1 == len(executed)


@PROTOCOLS
@pytest.mark.asyncio
async def test_truncated_responses_are_counted(native):
    steps = [("search_code", {"query": "x"}), ("read_file", {"path": "a"}), "FINAL"]
    llm = _FullLLM(steps, native, ["stop", "length", None])

    res = await _run(llm, [], native)

    assert res.truncated_responses == 1
    assert res.tool_calls == 2
    assert res.turns == 3


@PROTOCOLS
@pytest.mark.asyncio
async def test_reported_and_not_truncated_is_zero_not_unknown(native):
    llm = _FullLLM([("search_code", {"query": "x"}), "FINAL"], native, ["stop", "stop"])

    res = await _run(llm, [], native)

    assert res.truncated_responses == 0


@PROTOCOLS
@pytest.mark.asyncio
async def test_no_finish_reason_ever_reported_is_unknown(native):
    llm = _FullLLM([("search_code", {"query": "x"}), "FINAL"], native, [None, None])

    res = await _run(llm, [], native)

    assert res.truncated_responses is None


@PROTOCOLS
@pytest.mark.asyncio
async def test_anthropic_style_reason_counts_as_truncated(native):
    llm = _FullLLM(["FINAL"], native, ["max_tokens"])

    res = await _run(llm, [], native)

    assert res.truncated_responses == 1


@PROTOCOLS
@pytest.mark.asyncio
async def test_forced_final_answer_truncation_is_counted(native):
    steps = [("search_code", {"query": "a"}), ("search_code", {"query": "b"}), "FINAL"]
    llm = _FullLLM(steps, native, ["stop", "stop", "length"])

    res = await _run(llm, [], native, max_turns=2)

    assert res.hit_cap is True
    assert res.truncated_responses == 1
    assert res.tool_call_counts == {"search_code": 2, "read_file": 0}


@PROTOCOLS
@pytest.mark.asyncio
async def test_figures_survive_an_llm_failure_mid_run(native):
    class _Failing(_ChatOnlyLLM):
        async def chat(self, messages, *, tools=None, max_tokens=None):
            if not self.steps:
                raise RuntimeError("context overflow")
            return await super().chat(messages, tools=tools, max_tokens=max_tokens)

    steps = [("search_code", {"query": "x"}), ("search_code", {"query": "x"})]

    res = await _run(_Failing(steps, native), [], native)

    assert res.error
    assert res.tool_call_counts == {"search_code": 2, "read_file": 0}
    assert res.repeated_tool_calls == 1
