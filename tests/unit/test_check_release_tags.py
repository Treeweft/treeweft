"""Release tags: CalVer shape, the SemVer tag on the same commit, and the month rule (ADR-004 §4)."""
import importlib.util
import pathlib
import subprocess

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "check_release_tags.py"
_spec = importlib.util.spec_from_file_location("check_release_tags", _SCRIPT)
crt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crt)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for var, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(var, value)
    # Hermetic against the developer's own git config: a global commit.gpgsign
    # or tag.gpgSign would otherwise fail these commits/tags in CI or locally.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def release(repo, version, *tags):
    (repo / "pyproject.toml").write_text(f'[project]\nname = "treeweft"\nversion = "{version}"\n')
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", version], check=True)
    for tag in tags:
        subprocess.run(["git", "-C", str(repo), "tag", tag], check=True)


def test_a_well_formed_release_passes(repo):
    release(repo, "1.0.0", "v1.0.0", "v2026.10.1")
    assert crt.check(repo, "v2026.10.1") == []


@pytest.mark.parametrize("bad", ["v2026.09.1", "2026.10.1", "v2026.13.1", "v2026.10.1.0", "v1.0.0"])
def test_malformed_calver_tag_is_refused(repo, bad):
    release(repo, "1.0.0", "v1.0.0")
    assert "is not CalVer" in crt.check(repo, bad)[0]


def test_missing_semver_tag_is_refused(repo):
    release(repo, "1.0.0", "v2026.10.1")
    errors = crt.check(repo, "v2026.10.1")
    assert len(errors) == 1 and "v1.0.0" in errors[0] and "push v1.0.0 first" in errors[0]


def test_semver_tag_on_another_commit_is_refused(repo):
    release(repo, "1.0.0", "v1.0.0")
    release(repo, "1.0.0", "v2026.10.1")
    assert "points elsewhere" in crt.check(repo, "v2026.10.1")[0]


def test_non_semver_pyproject_is_refused(repo):
    release(repo, "2026.10.1", "v2026.10.1")
    assert "is not MAJOR.MINOR.PATCH SemVer" in crt.check(repo, "v2026.10.1")[0]


def test_major_bump_mid_month_is_refused(repo):
    release(repo, "1.0.0", "v1.0.0", "v2026.10.1")
    release(repo, "2.0.0", "v2.0.0", "v2026.10.15")
    errors = crt.check(repo, "v2026.10.15")
    assert len(errors) == 1 and "breaking release mid-month" in errors[0] and "v2026.10.1" in errors[0]


def test_minor_bump_mid_month_is_fine(repo):
    release(repo, "1.0.0", "v1.0.0", "v2026.10.1")
    release(repo, "1.1.0", "v1.1.0", "v2026.10.15")
    assert crt.check(repo, "v2026.10.15") == []


def test_major_bump_in_a_new_month_is_fine(repo):
    release(repo, "1.0.0", "v1.0.0", "v2026.10.1")
    release(repo, "2.0.0", "v2.0.0", "v2026.11.2")
    assert crt.check(repo, "v2026.11.2") == []


def test_month_prefix_does_not_confuse_january_with_october(repo):
    release(repo, "1.0.0", "v1.0.0", "v2027.1.5")
    release(repo, "2.0.0", "v2.0.0", "v2027.10.1")
    assert crt.check(repo, "v2027.10.1") == []


def test_pre_semver_release_in_same_month_is_ignored(repo):
    release(repo, "2026.9.23", "v2026.9.23")          # CalVer-era release
    release(repo, "1.0.0", "v1.0.0", "v2026.9.28")   # first SemVer release, same month
    assert crt.check(repo, "v2026.9.28") == []


def test_major_bump_on_non_ancestor_branch_is_refused(repo):
    # issue #31: a release cut from a branch that doesn't contain the month's
    # earlier release must still be compared against it.
    release(repo, "1.0.0", "v1.0.0")
    release(repo, "1.1.0", "v1.1.0", "v2026.10.1")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side", "v1.0.0"], check=True)
    release(repo, "2.0.0", "v2.0.0", "v2026.10.15")
    errors = crt.check(repo, "v2026.10.15")
    assert len(errors) == 1 and "breaking release mid-month" in errors[0] and "v2026.10.1" in errors[0]


def test_hotfix_after_non_ancestor_major_bump_is_refused(repo):
    # issue #31 reverse case: a 1.x hotfix from a maintenance branch, released
    # later in a month whose first release was 2.0.0, must also be refused.
    release(repo, "1.0.0", "v1.0.0")
    release(repo, "2.0.0", "v2.0.0", "v2026.10.1")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "maint", "v1.0.0"], check=True)
    release(repo, "1.0.1", "v1.0.1", "v2026.10.15")
    errors = crt.check(repo, "v2026.10.15")
    assert len(errors) == 1 and "breaking release mid-month" in errors[0] and "v2026.10.1" in errors[0]


def test_second_same_month_release_with_same_major_on_side_branch_is_fine(repo):
    # A legitimate second release in the month, cut from a non-ancestor branch,
    # must still pass when the major matches.
    release(repo, "1.0.0", "v1.0.0")
    release(repo, "1.1.0", "v1.1.0", "v2026.10.1")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side", "v1.0.0"], check=True)
    release(repo, "1.0.1", "v1.0.1", "v2026.10.15")
    assert crt.check(repo, "v2026.10.15") == []


def test_missing_pyproject_at_released_tag_is_a_clean_error(repo):
    # issue #33: a missing/unreadable pyproject.toml at the tag being released
    # must produce a clean ::error:: line, not a traceback.
    (repo / "README.md").write_text("no pyproject here")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "no pyproject"], check=True)
    subprocess.run(["git", "-C", str(repo), "tag", "v2026.10.1"], check=True)
    errors = crt.check(repo, "v2026.10.1")
    assert errors == ["pyproject.toml is missing or unreadable at v2026.10.1"]


def test_main_reports_missing_pyproject_without_traceback(repo, monkeypatch, capsys):
    (repo / "README.md").write_text("no pyproject here")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "no pyproject"], check=True)
    subprocess.run(["git", "-C", str(repo), "tag", "v2026.10.1"], check=True)
    monkeypatch.chdir(repo)
    exit_code = crt.main(["check_release_tags.py", "v2026.10.1"])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "::error::pyproject.toml is missing or unreadable at v2026.10.1" in out
    assert "Traceback" not in out


def test_earlier_tag_with_unreadable_pyproject_is_a_warning_not_a_failure(repo, capsys):
    # issue #33: an earlier same-month tag with no readable pyproject.toml must
    # be skipped with a warning, not fail the check.
    (repo / "README.md").write_text("no pyproject here")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "no pyproject"], check=True)
    subprocess.run(["git", "-C", str(repo), "tag", "v2026.10.1"], check=True)
    release(repo, "1.0.0", "v1.0.0", "v2026.10.15")
    errors = crt.check(repo, "v2026.10.15")
    out = capsys.readouterr().out
    assert errors == []
    assert "::warning::pyproject.toml is missing or unreadable at v2026.10.1" in out


@pytest.mark.parametrize("content", [
    '[project]\nname = "treeweft"\n',   # no version
    '[tool.other]\nx = 1\n',            # no [project] table
    'this is [not toml\n',               # not parseable
    '[project]\nversion = 1.0\n',        # version is not a string
    'project = "treeweft"\n',            # project is not a table
])
def test_pyproject_without_a_readable_version_is_a_clean_error(repo, content):
    (repo / "pyproject.toml").write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "bad pyproject"], check=True)
    subprocess.run(["git", "-C", str(repo), "tag", "v2026.10.1"], check=True)
    assert crt.check(repo, "v2026.10.1") == ["pyproject.toml is missing or unreadable at v2026.10.1"]


def test_same_day_second_release_with_a_new_major_is_refused(repo):
    release(repo, "1.0.0", "v1.0.0", "v2026.10.1")
    release(repo, "2.0.0", "v2.0.0", "v2026.10.1.1")
    errors = crt.check(repo, "v2026.10.1.1")
    assert len(errors) == 1 and "breaking release mid-month" in errors[0]


def test_later_release_in_the_month_does_not_fail_an_earlier_one(repo):
    """Re-running the check for the month's first release, after a later
    release with another major was (wrongly) tagged, blames the later one."""
    release(repo, "1.0.0", "v1.0.0", "v2026.10.1")
    release(repo, "2.0.0", "v2.0.0", "v2026.10.15")
    assert crt.check(repo, "v2026.10.1") == []
    assert len(crt.check(repo, "v2026.10.15")) == 1


def test_tag_that_does_not_exist_is_reported_as_such(repo):
    release(repo, "1.0.0", "v1.0.0", "v2026.10.1")
    assert crt.check(repo, "v2026.10.9") == ["tag v2026.10.9 does not exist"]
