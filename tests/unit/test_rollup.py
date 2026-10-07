"""Unit tests for benchmark rollup — Detroit-style, no mocks, no I/O."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from treeweft.domain.benchmark.rollup import (
    format_comparison_table,
    format_rollup_table,
    multi_repo_rollup,
)
from treeweft.domain.benchmark.significance import sign_test


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_summary(
    repo: str,
    n_queries: int,
    grep_tokens: float,
    tl_tokens: float,
    grep_recall5: float,
    tl_recall5: float,
    grep_corr: float,
    tl_corr: float,
    corr_wins: int,
    corr_losses: int,
    corr_ties: int,
    rec5_wins: int,
    rec5_losses: int,
    rec5_ties: int,
) -> dict:
    """Build a fake agentic summary dict in the Task-A dict win_rate format."""
    c_total = corr_wins + corr_losses + corr_ties
    r_total = rec5_wins + rec5_losses + rec5_ties
    tokens_saved_pct = (grep_tokens - tl_tokens) / grep_tokens * 100 if grep_tokens else 0.0
    token_ratio = tl_tokens / grep_tokens if grep_tokens else 0.0
    return {
        "repo": repo,
        "model": "test-model",
        "n_queries": n_queries,
        "arms": ["grep", "treeweft"],
        "grep": {
            "mean_total_tokens": grep_tokens,
            "mean_recall@5": grep_recall5,
            "mean_correctness": grep_corr,
        },
        "treeweft": {
            "mean_total_tokens": tl_tokens,
            "mean_recall@5": tl_recall5,
            "mean_correctness": tl_corr,
        },
        "comparison": {
            "token_ratio_treeweft_over_grep": round(token_ratio, 4),
            "tokens_saved_pct": round(tokens_saved_pct, 2),
            "correctness_win_rate_treeweft": {
                "win_rate": round(corr_wins / c_total, 4) if c_total else 0.0,
                "wins": corr_wins,
                "losses": corr_losses,
                "ties": corr_ties,
                "p_value": sign_test(corr_wins, corr_losses),
            },
            "recall@5_win_rate_treeweft": {
                "win_rate": round(rec5_wins / r_total, 4) if r_total else 0.0,
                "wins": rec5_wins,
                "losses": rec5_losses,
                "ties": rec5_ties,
                "p_value": sign_test(rec5_wins, rec5_losses),
            },
        },
    }


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def summary_a():
    return _make_summary(
        repo="repo-a",
        n_queries=10,
        grep_tokens=20000.0,
        tl_tokens=18000.0,
        grep_recall5=0.8,
        tl_recall5=0.9,
        grep_corr=3.5,
        tl_corr=4.0,
        corr_wins=6,
        corr_losses=2,
        corr_ties=2,
        rec5_wins=5,
        rec5_losses=3,
        rec5_ties=2,
    )


@pytest.fixture
def summary_b():
    return _make_summary(
        repo="repo-b",
        n_queries=20,
        grep_tokens=15000.0,
        tl_tokens=12000.0,
        grep_recall5=0.7,
        tl_recall5=0.75,
        grep_corr=3.0,
        tl_corr=3.2,
        corr_wins=10,
        corr_losses=5,
        corr_ties=5,
        rec5_wins=8,
        rec5_losses=7,
        rec5_ties=5,
    )


# ── multi_repo_rollup ────────────────────────────────────────────────────────


class TestMultiRepoRollup:
    def test_pooled_treeweft_tokens_weighted_average(self, summary_a, summary_b):
        """Pooled treeweft tokens = n_queries-weighted average."""
        rollup = multi_repo_rollup([summary_a, summary_b])
        pooled = rollup["pooled"]

        # n_a=10, tl_a=18000; n_b=20, tl_b=12000; total=30
        expected = (18000.0 * 10 + 12000.0 * 20) / 30
        assert pooled["treeweft_mean_total_tokens"] == round(expected, 4)

    def test_pooled_grep_tokens_weighted_average(self, summary_a, summary_b):
        rollup = multi_repo_rollup([summary_a, summary_b])
        pooled = rollup["pooled"]
        expected = (20000.0 * 10 + 15000.0 * 20) / 30
        assert pooled["grep_mean_total_tokens"] == round(expected, 4)

    def test_pooled_correctness_p_value_equals_sign_test_of_summed_counts(
        self, summary_a, summary_b
    ):
        """Pooled correctness p_value must equal sign_test(sum_wins, sum_losses)."""
        rollup = multi_repo_rollup([summary_a, summary_b])
        pooled = rollup["pooled"]

        # a: 6 wins, 2 losses; b: 10 wins, 5 losses
        total_wins = 6 + 10
        total_losses = 2 + 5
        expected_p = sign_test(total_wins, total_losses)
        assert pooled["correctness"]["p_value"] == round(expected_p, 4)

    def test_pooled_recall5_p_value_equals_sign_test_of_summed_counts(
        self, summary_a, summary_b
    ):
        rollup = multi_repo_rollup([summary_a, summary_b])
        pooled = rollup["pooled"]
        # a: 5 wins, 3 losses; b: 8 wins, 7 losses
        total_wins = 5 + 8
        total_losses = 3 + 7
        expected_p = sign_test(total_wins, total_losses)
        assert pooled["recall@5"]["p_value"] == round(expected_p, 4)

    def test_pooled_win_loss_tie_counts_are_summed(self, summary_a, summary_b):
        rollup = multi_repo_rollup([summary_a, summary_b])
        pooled = rollup["pooled"]
        assert pooled["correctness"]["wins"] == 6 + 10
        assert pooled["correctness"]["losses"] == 2 + 5
        assert pooled["correctness"]["ties"] == 2 + 5

    def test_n_repos_and_total_queries(self, summary_a, summary_b):
        rollup = multi_repo_rollup([summary_a, summary_b])
        assert rollup["pooled"]["n_repos"] == 2
        assert rollup["pooled"]["total_queries"] == 30

    def test_repos_list_length_matches_input(self, summary_a, summary_b):
        rollup = multi_repo_rollup([summary_a, summary_b])
        assert len(rollup["repos"]) == 2

    def test_repo_row_names_preserved(self, summary_a, summary_b):
        rollup = multi_repo_rollup([summary_a, summary_b])
        names = [r["repo"] for r in rollup["repos"]]
        assert "repo-a" in names
        assert "repo-b" in names

    def test_tokens_saved_pct_recomputed_from_pooled_means(self, summary_a, summary_b):
        rollup = multi_repo_rollup([summary_a, summary_b])
        pooled = rollup["pooled"]
        grep_tok = pooled["grep_mean_total_tokens"]
        tl_tok = pooled["treeweft_mean_total_tokens"]
        expected_saved = round((grep_tok - tl_tok) / grep_tok * 100, 2)
        assert pooled["tokens_saved_pct"] == expected_saved

    def test_single_summary_rollup(self, summary_a):
        """Single-summary rollup degenerates cleanly."""
        rollup = multi_repo_rollup([summary_a])
        pooled = rollup["pooled"]
        assert pooled["n_repos"] == 1
        assert pooled["total_queries"] == 10
        assert pooled["treeweft_mean_total_tokens"] == 18000.0

    def test_empty_summaries(self):
        rollup = multi_repo_rollup([])
        assert rollup["repos"] == []
        assert rollup["pooled"]["n_repos"] == 0
        assert rollup["pooled"]["total_queries"] == 0

    def test_summary_without_comparison_block_included(self):
        """Summaries lacking a comparison block are included but contribute 0 counts."""
        s = {
            "repo": "no-comp",
            "n_queries": 5,
            "arms": ["treeweft"],
            "treeweft": {
                "mean_total_tokens": 10000.0,
                "mean_recall@5": 0.6,
                "mean_correctness": 3.0,
            },
        }
        rollup = multi_repo_rollup([s])
        assert len(rollup["repos"]) == 1
        assert rollup["repos"][0]["repo"] == "no-comp"
        # No wins/losses from a missing comparison block
        assert rollup["pooled"]["correctness"]["wins"] == 0
        assert rollup["pooled"]["correctness"]["p_value"] == 1.0


# ── format_comparison_table ──────────────────────────────────────────────────


class TestFormatComparisonTable:
    def test_table_contains_pipe_characters(self, summary_a):
        table = format_comparison_table(summary_a)
        assert "|" in table

    def test_table_contains_arm_names(self, summary_a):
        table = format_comparison_table(summary_a)
        assert "grep" in table
        assert "treeweft" in table

    def test_table_contains_p_value(self, summary_a):
        table = format_comparison_table(summary_a)
        # p-value column should contain a numeric value
        assert "0." in table or "1.0" in table

    def test_table_contains_recall5_row(self, summary_a):
        table = format_comparison_table(summary_a)
        assert "recall@5" in table

    def test_table_contains_correctness_row(self, summary_a):
        table = format_comparison_table(summary_a)
        assert "correctness" in table

    def test_table_contains_tokens_row(self, summary_a):
        table = format_comparison_table(summary_a)
        assert "tokens" in table.lower()

    def test_no_comparison_block_returns_note(self):
        s = {"repo": "x", "n_queries": 5, "arms": ["treeweft"]}
        result = format_comparison_table(s)
        assert "grep-vs-treeweft" in result.lower() or "comparison" in result.lower()
        assert "|" not in result  # Not a table — just a note

    def test_legacy_flat_win_rate_handled_gracefully(self):
        """Old-format summaries with float win_rate values don't crash."""
        s = {
            "repo": "legacy",
            "n_queries": 10,
            "arms": ["grep", "treeweft"],
            "grep": {"mean_total_tokens": 10000.0, "mean_recall@5": 0.8, "mean_correctness": 3.5},
            "treeweft": {"mean_total_tokens": 8000.0, "mean_recall@5": 0.85, "mean_correctness": 3.8},
            "comparison": {
                "token_ratio_treeweft_over_grep": 0.8,
                "tokens_saved_pct": 20.0,
                "correctness_win_rate_treeweft": 0.6,  # legacy flat float
                "recall@5_win_rate_treeweft": 0.55,
            },
        }
        table = format_comparison_table(s)
        assert "|" in table
        assert "grep" in table


# ── format_rollup_table ──────────────────────────────────────────────────────


class TestFormatRollupTable:
    def test_contains_pooled_row(self, summary_a, summary_b):
        rollup = multi_repo_rollup([summary_a, summary_b])
        table = format_rollup_table(rollup)
        assert "Pooled" in table

    def test_contains_one_row_per_repo(self, summary_a, summary_b):
        rollup = multi_repo_rollup([summary_a, summary_b])
        table = format_rollup_table(rollup)
        assert "repo-a" in table
        assert "repo-b" in table

    def test_contains_pipe_characters(self, summary_a, summary_b):
        rollup = multi_repo_rollup([summary_a, summary_b])
        table = format_rollup_table(rollup)
        assert "|" in table

    def test_single_repo_table_has_pooled_row(self, summary_a):
        rollup = multi_repo_rollup([summary_a])
        table = format_rollup_table(rollup)
        assert "Pooled" in table
        assert "repo-a" in table

    def test_empty_rollup_still_has_pooled_row(self):
        rollup = multi_repo_rollup([])
        table = format_rollup_table(rollup)
        assert "Pooled" in table


# ── Repeat-call figures in the tables ────────────────────────────────────────

def _with_stats(summary: dict, *, grep: dict, treeweft: dict) -> dict:
    summary["grep"].update({"mean_turns": 6.2, **grep})
    summary["treeweft"].update({"mean_turns": 4.1, **treeweft})
    return summary


@pytest.fixture
def summary_stats(summary_a):
    return _with_stats(
        summary_a,
        grep={"looped_share": 0.08, "mean_repeated_tool_calls": 0.42,
              "mean_tool_calls_by_tool": {"grep": 3.1, "glob": 0.8, "read_file": 2.4}},
        treeweft={"looped_share": 0.02, "mean_repeated_tool_calls": 0.1,
                  "mean_tool_calls_by_tool": {"search_code": 1.7, "read_file": 1.2}},
    )


def _old_table_lines(summary: dict) -> list[str]:
    """The table as it was before the feature: header + the three rows."""
    return format_comparison_table(summary).splitlines()[:8]


class TestComparisonTableCallStats:
    def test_new_rows_are_present(self, summary_stats):
        table = format_comparison_table(summary_stats)
        assert "| Mean turns | 6.20 | 4.10 | — | — |" in table
        assert "| Looped queries | 8.0% | 2.0% | — | — |" in table
        assert "| Mean repeated calls | 0.42 | 0.10 | — | — |" in table

    def test_calls_per_tool_lists_each_arms_own_tools(self, summary_stats):
        table = format_comparison_table(summary_stats)
        assert "Calls per tool (mean per query)" in table
        assert "- grep: glob 0.8, grep 3.1, read_file 2.4" in table
        assert "- treeweft: read_file 1.2, search_code 1.7" in table

    def test_existing_rows_are_untouched_and_come_first(self, summary_a):
        import copy

        plain = copy.deepcopy(summary_a)
        with_stats = _with_stats(
            copy.deepcopy(summary_a),
            grep={"looped_share": 0.5, "mean_repeated_tool_calls": 1.0,
                  "mean_tool_calls_by_tool": {"grep": 1.0}},
            treeweft={"looped_share": 0.0, "mean_repeated_tool_calls": 0.0,
                      "mean_tool_calls_by_tool": {"search_code": 1.0}},
        )
        assert _old_table_lines(with_stats) == _old_table_lines(plain)
        assert _old_table_lines(plain)[-1].startswith("| Mean correctness |")

    def test_summary_from_before_the_feature_shows_not_available(self, summary_a):
        table = format_comparison_table(summary_a)
        assert "| Mean turns | n/a | n/a | — | — |" in table
        assert "| Looped queries | n/a | n/a | — | — |" in table
        assert "| Mean repeated calls | n/a | n/a | — | — |" in table
        assert "- grep: n/a" in table
        assert "- treeweft: n/a" in table

    def test_zero_is_shown_as_zero_not_as_not_available(self, summary_a):
        s = _with_stats(
            summary_a,
            grep={"looped_share": 0.0, "mean_repeated_tool_calls": 0.0,
                  "mean_tool_calls_by_tool": {}},
            treeweft={"looped_share": 0.0, "mean_repeated_tool_calls": 0.0,
                      "mean_tool_calls_by_tool": {}},
        )
        table = format_comparison_table(s)
        assert "| Looped queries | 0.0% | 0.0% | — | — |" in table
        assert "- grep: none" in table


class TestRollupCallStats:
    def _rollup(self, summary_stats, summary_b):
        return multi_repo_rollup([summary_stats, summary_b])

    def test_per_repo_fields(self, summary_stats, summary_b):
        with_stats, without = self._rollup(summary_stats, summary_b)["repos"]
        assert with_stats["grep_looped_share"] == 0.08
        assert with_stats["treeweft_looped_share"] == 0.02
        assert with_stats["grep_mean_repeated_tool_calls"] == 0.42
        assert with_stats["treeweft_mean_repeated_tool_calls"] == 0.1
        assert with_stats["treeweft_mean_tool_calls_by_tool"] == {
            "search_code": 1.7, "read_file": 1.2}
        for key in ("grep_looped_share", "treeweft_looped_share",
                    "grep_mean_repeated_tool_calls",
                    "treeweft_mean_repeated_tool_calls",
                    "grep_mean_tool_calls_by_tool",
                    "treeweft_mean_tool_calls_by_tool"):
            assert without[key] is None, key

    def test_pooled_is_over_repos_that_have_the_figures(self, summary_stats, summary_b):
        pooled = self._rollup(summary_stats, summary_b)["pooled"]
        # Only summary_stats measured it, so pooled equals its own figures.
        assert pooled["grep_looped_share"] == 0.08
        assert pooled["treeweft_mean_repeated_tool_calls"] == 0.1

    def test_pooled_is_weighted_by_query_count(self, summary_a, summary_b):
        a = _with_stats(summary_a,
                        grep={"looped_share": 0.1, "mean_repeated_tool_calls": 1.0},
                        treeweft={"looped_share": 0.0, "mean_repeated_tool_calls": 0.0})
        b = _with_stats(summary_b,
                        grep={"looped_share": 0.4, "mean_repeated_tool_calls": 2.0},
                        treeweft={"looped_share": 0.2, "mean_repeated_tool_calls": 1.0})
        na, nb = a["n_queries"], b["n_queries"]
        pooled = multi_repo_rollup([a, b])["pooled"]
        assert pooled["grep_looped_share"] == pytest.approx(
            (0.1 * na + 0.4 * nb) / (na + nb), abs=1e-4)
        assert pooled["treeweft_mean_repeated_tool_calls"] == pytest.approx(
            nb / (na + nb), abs=1e-4)

    def test_pooled_not_available_when_no_repo_has_figures(self, summary_a, summary_b):
        pooled = multi_repo_rollup([summary_a, summary_b])["pooled"]
        assert pooled["grep_looped_share"] is None
        assert pooled["treeweft_mean_repeated_tool_calls"] is None

    def test_existing_rollup_fields_are_unchanged(self, summary_a, summary_b):
        import copy

        before = multi_repo_rollup([copy.deepcopy(summary_a), summary_b])
        after = multi_repo_rollup([
            _with_stats(copy.deepcopy(summary_a),
                        grep={"looped_share": 0.9, "mean_repeated_tool_calls": 9.0},
                        treeweft={"looped_share": 0.9, "mean_repeated_tool_calls": 9.0}),
            summary_b,
        ])
        new = ("looped_share", "repeated_tool_calls", "tool_calls_by_tool")
        for key, value in before["pooled"].items():
            if not key.endswith(new):
                assert after["pooled"][key] == value, key
        for b_repo, a_repo in zip(before["repos"], after["repos"]):
            for key, value in b_repo.items():
                if not key.endswith(new):
                    assert a_repo[key] == value, key

    def test_table_has_two_new_columns(self, summary_stats, summary_b):
        table = format_rollup_table(self._rollup(summary_stats, summary_b))
        header, _sep, first, second, pooled = table.splitlines()
        assert header.endswith("| corr p | looped g/t | repeats g/t |")
        assert first.endswith("| 8.0% / 2.0% | 0.42 / 0.10 |")
        assert second.endswith("| n/a | n/a |")
        assert pooled.endswith("| 8.0% / 2.0% | 0.42 / 0.10 |")

    def test_existing_columns_are_unchanged(self, summary_a, summary_b):
        table = format_rollup_table(multi_repo_rollup([summary_a, summary_b]))
        header, sep, *rows = table.splitlines()
        assert header.startswith(
            "| Repo | n | grep tok | treeweft tok | saved% "
            "| r@5 win-rt | r@5 p | corr win-rt | corr p |")
        assert sep.startswith(
            "|------|--:|---------:|-------------:|------:"
            "|----------:|------:|------------:|-------:|")
        assert all(r.endswith("| n/a | n/a |") for r in rows)
        assert all(r.count("|") == header.count("|") for r in rows)
