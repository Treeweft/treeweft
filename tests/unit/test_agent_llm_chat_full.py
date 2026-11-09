"""Adapter tests for AgentLLM.chat_full — the finish reason alongside what
chat() already returns. The only fake is the HTTP boundary."""
from __future__ import annotations

import httpx
import pytest

from treeweft.adapters.benchmark import agent_llm as agent_llm_mod
from treeweft.adapters.benchmark.agent_llm import AgentLLM, ChatResult

_USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
_MESSAGES = [{"role": "user", "content": "hi"}]


def _config(vendor: str = "") -> dict:
    return {"url": "http://llm.test/v1", "key": "k", "model": "m-requested",
            "timeout": 5.0, "vendor": vendor}


@pytest.fixture
def served(monkeypatch):
    """Serve canned JSON for every request; records each request made."""
    state = {"body": {}, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return httpx.Response(200, json=state["body"])

    real_client = httpx.AsyncClient

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(agent_llm_mod.httpx, "AsyncClient", client)
    return state


def _openai_body(finish_reason="stop", **message) -> dict:
    choice = {"message": {"content": "the answer", **message}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"model": "m-served", "choices": [choice], "usage": dict(_USAGE)}


@pytest.mark.asyncio
async def test_openai_compatible_finish_reason(served):
    served["body"] = _openai_body("length")
    llm = AgentLLM(config=_config())

    result = await llm.chat_full(_MESSAGES)

    assert isinstance(result, ChatResult)
    assert result.content == "the answer"
    assert result.usage == _USAGE
    assert result.tool_calls is None
    assert result.finish_reason == "length"
    assert llm.served_models == {"m-served"}


@pytest.mark.asyncio
async def test_missing_finish_reason_is_none(served):
    served["body"] = _openai_body(None)

    result = await AgentLLM(config=_config()).chat_full(_MESSAGES)

    assert result.finish_reason is None


@pytest.mark.asyncio
async def test_tool_calls_are_carried(served):
    tcs = [{"id": "c1", "type": "function",
            "function": {"name": "search_code", "arguments": "{}"}}]
    served["body"] = _openai_body("tool_calls", content=None, tool_calls=tcs)

    result = await AgentLLM(config=_config()).chat_full(_MESSAGES, tools=[])

    assert result.tool_calls == tcs
    assert result.content == ""
    assert result.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_native_anthropic_stop_reason(served, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_USE_COMPAT", raising=False)
    served["body"] = {
        "model": "claude-served",
        "content": [{"type": "text", "text": "the answer"}],
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    llm = AgentLLM(config=_config(vendor="anthropic"))

    result = await llm.chat_full(_MESSAGES)

    assert result.content == "the answer"
    assert result.finish_reason == "max_tokens"
    assert result.usage["completion_tokens"] == 5
    assert llm.served_models == {"claude-served"}
    assert served["requests"][0].url.path.endswith("/messages")


@pytest.mark.asyncio
async def test_chat_still_returns_three_values_from_one_request(served):
    served["body"] = _openai_body("length")
    llm = AgentLLM(config=_config())

    content, usage, tool_calls = await llm.chat(_MESSAGES)

    assert (content, usage, tool_calls) == ("the answer", _USAGE, None)
    assert len(served["requests"]) == 1


@pytest.mark.asyncio
async def test_no_per_call_state_is_kept_on_the_client(served):
    """Arms and judges share one client under asyncio.gather, so a
    "last finish reason" attribute would be racy."""
    served["body"] = _openai_body("length")
    llm = AgentLLM(config=_config())
    before = set(vars(llm))

    await llm.chat_full(_MESSAGES)

    assert set(vars(llm)) == before
