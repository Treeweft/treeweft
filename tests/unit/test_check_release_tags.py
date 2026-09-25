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
