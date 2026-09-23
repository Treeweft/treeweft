"""Pre-rebrand agentic results (arm "treeloom", keys "treeloom_*") still roll up."""
from treeweft.domain.benchmark.legacy_names import upgrade_legacy_names
from treeweft.domain.benchmark.rollup import multi_repo_rollup


def test_summary_arm_and_comparison_keys_are_renamed():
    old = {
        "repo": "featbit",
        "n_queries": 2,
        "treeloom": {"mean_total_tokens": 100.0},
        "comparison": {"treeloom_mean_total_tokens": 100.0, "grep_mean_total_tokens": 150.0},
    }
    new = upgrade_legacy_names(old)
    assert new["treeweft"] == {"mean_total_tokens": 100.0}
    assert new["comparison"]["treeweft_mean_total_tokens"] == 100.0
    assert new["comparison"]["grep_mean_total_tokens"] == 150.0
    assert "treeloom" not in new


def test_row_arm_values_are_renamed_but_free_text_is_not():
    row = {"arm": "treeloom-facet", "answer": "see treeloom/foo.py"}
    new = upgrade_legacy_names(row)
    assert new["arm"] == "treeweft-facet"
    assert new["answer"] == "see treeloom/foo.py"


def test_current_names_pass_through_unchanged():
    cur = {"treeweft": {"x": 1}, "arms": ["grep", "treeweft"]}
    assert upgrade_legacy_names(cur) == cur


def test_arm_lists_are_renamed():
    assert upgrade_legacy_names({"arms": ["grep", "treeloom"]}) == {"arms": ["grep", "treeweft"]}


def test_old_summary_contributes_to_rollup():
    old = {"repo": "r", "n_queries": 4, "grep": {"mean_total_tokens": 200.0},
           "treeloom": {"mean_total_tokens": 100.0}}
    pooled = multi_repo_rollup([upgrade_legacy_names(old)])["pooled"]
    assert pooled["treeweft_mean_total_tokens"] == 100.0
