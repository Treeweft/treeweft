"""The judge marks a verdict that was cut off at the token limit.

A cut-off verdict is usually broken JSON, so the marker must be set whatever
`parse_ok` turns out to be. Scores are computed exactly as before.
"""
from __future__ import annotations

import pytest

from treeweft.adapters.benchmark.agent_llm import ChatResult
from treeweft.application.benchmark import judge as judge_mod
from treeweft.application.benchmark.judge import judge_answer
from treeweft.domain.benchmark.judge_schema import JudgeScore, parse_judge_json

_GOOD = '{"correctness": 4, "completeness": 3, "rationale": "close"}'
_CUT_OFF = '{"correctness": 4, "completeness": 3, "rationale": "the candidate ident'


class _FullJudge:
    def __init__(self, content: str, finish_reason):
        self.content, self.finish_reason = content, finish_reason
        self.calls = 0

    async def chat_full(self, messages, *, tools=None, max_tokens=None):
        self.calls += 1
        return ChatResult(content=self.content, usage=None, tool_calls=None,
                          finish_reason=self.finish_reason)


class _ChatOnlyJudge:
    def __init__(self, content: str):
        self.content = content

    async def chat(self, messages, *, tools=None, max_tokens=None):
        return self.content, None, None


class _FailingJudge:
    calls = 0

    async def chat(self, messages, *, tools=None, max_tokens=None):
        self.calls += 1
        raise RuntimeError("read timeout")


@pytest.mark.asyncio
async def test_ordinary_verdict_is_not_truncated():
    score = await judge_answer("q", "gold", "candidate", _FullJudge(_GOOD, "stop"))

    assert (score.correctness, score.completeness, score.parse_ok) == (4, 3, True)
    assert score.truncated is False


@pytest.mark.asyncio
async def test_truncated_but_parseable_verdict_is_marked():
    score = await judge_answer("q", "gold", "candidate", _FullJudge(_GOOD, "length"))

    assert (score.correctness, score.completeness, score.parse_ok) == (4, 3, True)
    assert score.truncated is True


@pytest.mark.asyncio
async def test_truncated_and_unparseable_verdict_is_still_marked():
    llm = _FullJudge(_CUT_OFF, "length")

    score = await judge_answer("q", "gold", "candidate", llm)

    # Scored exactly as an unparseable verdict always was …
    unmarked = parse_judge_json(_CUT_OFF)
    assert score.parse_ok is False
    assert (score.correctness, score.completeness, score.rationale) == (
        unmarked.correctness, unmarked.completeness, unmarked.rationale)
    # … and now also identifiable as cut off, without an extra call.
    assert score.truncated is True
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_unknown_finish_reason_leaves_marker_unknown():
    score = await judge_answer("q", "gold", "candidate", _FullJudge(_GOOD, None))

    assert score.truncated is None


@pytest.mark.asyncio
async def test_chat_only_client_leaves_marker_unknown():
    score = await judge_answer("q", "gold", "candidate", _ChatOnlyJudge(_GOOD))

    assert (score.correctness, score.parse_ok) == (4, True)
    assert score.truncated is None


@pytest.mark.asyncio
async def test_no_call_made_leaves_marker_unknown():
    llm = _FullJudge(_GOOD, "length")

    no_gold = await judge_answer("q", "  ", "candidate", llm)
    no_candidate = await judge_answer("q", "gold", "", llm)

    assert no_gold.truncated is None and no_gold.parse_ok is False
    assert no_candidate.truncated is None and no_candidate.parse_ok is True
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_failed_call_leaves_marker_unknown(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(judge_mod.asyncio, "sleep", no_sleep)
    llm = _FailingJudge()

    score = await judge_answer("q", "gold", "candidate", llm)

    assert score.parse_ok is False
    assert score.truncated is None
    assert llm.calls == 2  # the existing single retry is unchanged


def test_as_dict_carries_the_marker():
    assert JudgeScore(4, 3, "r", True).as_dict() == {
        "correctness": 4, "completeness": 3, "rationale": "r",
        "parse_ok": True, "truncated": None,
    }
    assert JudgeScore(4, 3, "r", True, truncated=True).as_dict()["truncated"] is True
