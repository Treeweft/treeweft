"""Gold answers record whether they were cut off at the token limit.

A cut-off gold answer is cached and then used as the reference for every arm
on that query, so it has to be findable. The only fake is the LLM boundary;
the cache is a real file in a temp directory.
"""
from __future__ import annotations

import json

import pytest

from treeweft.adapters.benchmark.agent_llm import ChatResult
from treeweft.application.benchmark import gold as gold_mod
from treeweft.application.benchmark.gold import build_gold, gold_path


class _Answerer:
    """`finish_reasons` maps a query id (found in the prompt) to a reason."""

    def __init__(self, finish_reasons: dict):
        self.finish_reasons = finish_reasons
        self.calls = 0

    async def chat_full(self, messages, *, tools=None, max_tokens=None):
        self.calls += 1
        prompt = messages[-1]["content"]
        reason = next(r for qid, r in self.finish_reasons.items() if qid in prompt)
        return ChatResult(content="<think>x</think>the reference answer",
                          usage=None, tool_calls=None, finish_reason=reason)


class _ChatOnlyAnswerer:
    async def chat(self, messages, *, tools=None, max_tokens=None):
        return "the reference answer", None, None


@pytest.fixture
def queries(tmp_path, monkeypatch):
    monkeypatch.setattr(gold_mod, "GOLD_DIR", tmp_path / "gold")
    src = tmp_path / "foo.py"
    src.write_text("def foo():\n    return 1\n")
    return [
        {"id": qid, "query": f"what does {qid} do?", "relevant_files": [str(src)]}
        for qid in ("q-cut", "q-ok", "q-unknown")
    ]


_REASONS = {"q-cut": "length", "q-ok": "stop", "q-unknown": None}


@pytest.mark.asyncio
async def test_each_gold_record_carries_the_marker(queries):
    gold = await build_gold(queries, "repo", _Answerer(_REASONS))

    assert gold["q-cut"]["truncated"] is True
    assert gold["q-ok"]["truncated"] is False
    assert gold["q-unknown"]["truncated"] is None
    # The answer itself is produced exactly as before.
    assert gold["q-cut"]["gold_answer"] == "the reference answer"


@pytest.mark.asyncio
async def test_marker_is_written_to_the_cache_and_read_back(queries):
    await build_gold(queries, "repo", _Answerer(_REASONS))

    on_disk = {json.loads(line)["id"]: json.loads(line)
               for line in gold_path("repo").read_text().splitlines()}
    assert on_disk["q-cut"]["truncated"] is True

    second = _Answerer(_REASONS)
    gold = await build_gold(queries, "repo", second)
    assert second.calls == 0           # served from the cache, not regenerated
    assert gold["q-cut"]["truncated"] is True


@pytest.mark.asyncio
async def test_cut_off_gold_answers_are_reported_by_id(queries, capsys):
    await build_gold(queries, "repo", _Answerer(_REASONS))

    out = capsys.readouterr().out
    assert "1 gold answer(s) cut off at the token limit" in out
    assert "q-cut" in out
    assert "q-ok" not in out


@pytest.mark.asyncio
async def test_cached_cut_off_answers_are_reported_on_every_run(queries, capsys):
    await build_gold(queries, "repo", _Answerer(_REASONS))
    capsys.readouterr()

    await build_gold(queries, "repo", _Answerer(_REASONS))

    assert "q-cut" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_nothing_is_reported_when_none_are_cut_off(queries, capsys):
    await build_gold(queries[1:2], "repo", _Answerer(_REASONS))

    assert "cut off" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_gold_cached_before_the_marker_is_unknown_and_kept(queries):
    path = gold_path("repo")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "id": "q-cut", "query": "what does q-cut do?",
        "gold_answer": "an older reference", "relevant_files": [], "entity": {},
    }) + "\n")
    answerer = _Answerer(_REASONS)

    gold = await build_gold(queries[:1], "repo", answerer)

    assert answerer.calls == 0
    assert gold["q-cut"]["gold_answer"] == "an older reference"
    assert gold["q-cut"].get("truncated") is None


@pytest.mark.asyncio
async def test_chat_only_client_leaves_the_marker_unknown(queries):
    gold = await build_gold(queries[:1], "repo", _ChatOnlyAnswerer())

    assert gold["q-cut"]["gold_answer"] == "the reference answer"
    assert gold["q-cut"]["truncated"] is None


@pytest.mark.asyncio
async def test_regen_replaces_an_unknown_marker(queries):
    await build_gold(queries[:1], "repo", _ChatOnlyAnswerer())

    gold = await build_gold(queries[:1], "repo", _Answerer(_REASONS), regen=True)

    assert gold["q-cut"]["truncated"] is True
