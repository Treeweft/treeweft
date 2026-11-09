"""Domain tests for tool_call_stats — per-tool counts and exact repeats."""
import pytest

from treeweft.domain.benchmark.tool_call_stats import LOOP_THRESHOLD, ToolCallTally


def _tally(*calls) -> ToolCallTally:
    t = ToolCallTally()
    for tool, args in calls:
        t.record(tool, args)
    return t


def test_empty_tally():
    t = ToolCallTally()
    assert t.counts_by_tool == {}
    assert t.repeated == 0
    assert t.looped is False


def test_loop_threshold_is_three():
    assert LOOP_THRESHOLD == 3


def test_three_identical_and_two_different():
    t = _tally(
        ("A", {"q": "x"}), ("A", {"q": "x"}), ("A", {"q": "x"}),
        ("B", {"path": "a.py"}), ("B", {"path": "b.py"}),
    )
    assert t.repeated == 2
    assert t.looped is True
    assert t.counts_by_tool == {"A": 3, "B": 2}


def test_two_identical_calls_repeat_but_do_not_loop():
    t = _tally(("A", {"q": "x"}), ("A", {"q": "x"}))
    assert t.repeated == 1
    assert t.looped is False


def test_four_different_queries_are_not_repeats():
    t = _tally(*[("search_code", {"query": f"q{i}"}) for i in range(4)])
    assert t.counts_by_tool == {"search_code": 4}
    assert t.repeated == 0
    assert t.looped is False


def test_key_order_is_ignored():
    t = _tally(
        ("A", {"a": 1, "b": {"x": 1, "y": 2}}),
        ("A", {"b": {"y": 2, "x": 1}, "a": 1}),
    )
    assert t.repeated == 1


@pytest.mark.parametrize("first,second", [
    ({"q": "x"}, {"q": "y"}),                    # a value differs
    ({"q": ["a", "b"]}, {"q": ["b", "a"]}),      # list order is significant
    ({"q": "x"}, {"q": "x", "top_k": 5}),        # an extra key
    ({"q": "x"}, {"q": "x "}),                   # whitespace inside a value
    ({"n": 1}, {"n": "1"}),                      # a different type
])
def test_any_other_difference_is_a_different_call(first, second):
    assert _tally(("A", first), ("A", second)).repeated == 0


def test_same_arguments_to_different_tools_are_different_calls():
    t = _tally(("A", {"q": "x"}), ("B", {"q": "x"}))
    assert t.repeated == 0
    assert t.counts_by_tool == {"A": 1, "B": 1}


def test_empty_and_missing_arguments_are_the_same_call():
    assert _tally(("A", {}), ("A", None)).repeated == 1


def test_unserialisable_arguments_do_not_raise():
    marker = object()
    t = _tally(("A", {"x": marker}), ("A", {"x": {1, 2}}))
    assert t.counts_by_tool == {"A": 2}


def test_invariants_hold():
    calls = [
        ("A", {"q": "x"}), ("A", {"q": "x"}), ("A", {"q": "x"}), ("A", {"q": "x"}),
        ("A", {"q": "y"}), ("B", {"p": 1}), ("B", {"p": 1}), ("C", {}),
    ]
    t = _tally(*calls)
    total = len(calls)
    distinct = 4  # A/x, A/y, B/1, C
    assert sum(t.counts_by_tool.values()) == total
    assert t.repeated == total - distinct
    assert t.looped is True and t.repeated >= 2


def test_exposed_tools_never_called_are_reported_as_zero():
    t = ToolCallTally(["search_code", "read_file"])
    t.record("search_code", {"query": "x"})

    assert t.counts_by_tool == {"search_code": 1, "read_file": 0}
    assert t.repeated == 0
    assert ToolCallTally(["a", "b"]).counts_by_tool == {"a": 0, "b": 0}


def test_counts_by_tool_is_a_copy():
    t = _tally(("A", {}))
    t.counts_by_tool["A"] = 99
    assert t.counts_by_tool == {"A": 1}
