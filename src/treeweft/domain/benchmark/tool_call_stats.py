"""Benchmark domain: per-run tool-call tallies — pure, no I/O.

Exact repeats show an agent that is stuck re-issuing the same call. Calls per
tool show an agent that searched again with different wording, or read more
files, to compensate for a thinner answer — which exact repeats alone miss.
"""
from __future__ import annotations

import json
from collections import Counter

# The same tool with the same arguments this many times in one run is a loop.
LOOP_THRESHOLD = 3


class ToolCallTally:
    """Executed tool calls of one agent run.

    Two calls are the same when the tool name matches and the arguments are
    equal ignoring key order and whitespace. There is no similarity matching:
    any other difference makes them different calls.
    """

    def __init__(self) -> None:
        self._by_tool: Counter[str] = Counter()
        self._by_call: Counter[tuple[str, str]] = Counter()

    def record(self, tool: str, args: dict | None) -> None:
        key = json.dumps(args or {}, sort_keys=True, separators=(",", ":"),
                         default=str)
        self._by_tool[tool] += 1
        self._by_call[(tool, key)] += 1

    @property
    def counts_by_tool(self) -> dict[str, int]:
        return dict(self._by_tool)

    @property
    def repeated(self) -> int:
        """Calls whose tool and arguments equal an earlier call's."""
        return sum(n - 1 for n in self._by_call.values())

    @property
    def looped(self) -> bool:
        return any(n >= LOOP_THRESHOLD for n in self._by_call.values())
