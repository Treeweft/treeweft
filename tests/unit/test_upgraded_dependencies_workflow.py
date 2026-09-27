"""The weekly canary tests upgraded dependencies and never gates a merge (#13)."""
import pathlib
import re
import subprocess

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"


def _load(name):
    wf = yaml.safe_load((WORKFLOWS / name).read_text())
    # PyYAML parses the top-level `on:` key as the boolean True, not the string "on".
    return wf, wf.get("on", wf.get(True))


def _steps(wf):
    return wf["jobs"]["unit-tests"]["steps"]


def _runs(wf):
    return [str(s.get("run", "")) for s in _steps(wf)]


def test_runs_weekly_and_on_demand():
    _, on = _load("upgraded-dependencies.yml")
    crons = [entry["cron"] for entry in on["schedule"]]
    assert len(crons) == 1
    minute, hour, day_of_month, month, day_of_week = crons[0].split()
    assert (day_of_month, month) == ("*", "*")
    assert day_of_week in {"0", "1", "2", "3", "4", "5", "6"}  # one day: weekly
    assert minute.isdigit() and hour.isdigit()
    assert "workflow_dispatch" in on


def test_never_runs_on_pull_requests_other_than_changes_to_itself():
    _, on = _load("upgraded-dependencies.yml")
    assert "push" not in on
    assert on["pull_request"]["paths"] == [".github/workflows/upgraded-dependencies.yml"]


def test_upgrades_before_installing_and_tests_what_it_installed():
    wf, _ = _load("upgraded-dependencies.yml")
    runs = _runs(wf)
    upgrade = next(i for i, r in enumerate(runs) if "uv lock --upgrade" in r)
    install = next(i for i, r in enumerate(runs) if "uv sync" in r)
    tests = next(i for i, r in enumerate(runs) if "pytest tests/unit" in r)
    assert upgrade < install < tests
    assert "--no-sync" in runs[tests]  # a sync here would install something else


def test_summary_is_written_even_when_a_step_before_it_failed():
    """The summary names the culprit, so a failed run is the one that needs it."""
    wf, _ = _load("upgraded-dependencies.yml")
    summary = next(s for s in _steps(wf) if "GITHUB_STEP_SUMMARY" in str(s.get("run", "")))
    assert summary["if"] == "always()"
    assert "scripts/ci/upgraded_packages_summary.sh" in summary["run"]
    assert _steps(wf)[-1] is summary


# ── scripts/ci/upgraded_packages_summary.sh ──

SUMMARY = ROOT / "scripts/ci/upgraded_packages_summary.sh"
CHANGES = """\
Updated fastapi v0.138.0 -> v0.141.1
Updated Typing_Extensions v4.14.0 -> v4.15.0
Updated torch v2.9.0 -> v2.10.0
Added cloudpickle v3.1.2
Removed opik v1.0.0
"""


def _summary(tmp_path, changes, freeze):
    log = tmp_path / "changes.log"
    log.write_text(changes)
    out = subprocess.run(
        ["bash", str(SUMMARY), str(log)],
        env={"PATH": "/usr/bin:/bin", "PYTHON": "3.11", "FREEZE_CMD": freeze},
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def _freeze(tmp_path, *lines):
    listing = tmp_path / "freeze.txt"
    listing.write_text("\n".join(lines) + "\n")
    return f"cat {listing}"


def test_summary_lists_only_the_packages_the_job_installed(tmp_path):
    """The lockfile covers every extra. A package from an extra this job
    does not install (torch) cannot be why its tests failed."""
    freeze = _freeze(tmp_path, "fastapi==0.141.1", "typing-extensions==4.15.0", "cloudpickle==3.1.2", "pytest==9.1.1")

    out = _summary(tmp_path, CHANGES, freeze)

    assert "3 of the 5 packages" in out
    assert "Updated fastapi v0.138.0 -> v0.141.1" in out
    assert "Added cloudpickle v3.1.2" in out
    assert "torch" not in out and "opik" not in out
    assert "python 3.11" in out


def test_summary_matches_names_whatever_their_case_or_separator(tmp_path):
    out = _summary(tmp_path, CHANGES, _freeze(tmp_path, "typing.extensions==4.15.0"))

    assert "1 of the 5 packages" in out
    assert "Updated Typing_Extensions v4.14.0 -> v4.15.0" in out


def test_summary_lists_every_change_when_the_environment_cannot_be_read(tmp_path):
    """The install step failed: there is nothing to narrow the list by."""
    out = _summary(tmp_path, CHANGES, "false")

    assert "every package that differs from uv.lock is listed (5)" in out
    assert "torch" in out and "opik" in out


def test_summary_says_so_when_nothing_was_upgraded(tmp_path):
    out = _summary(tmp_path, "", _freeze(tmp_path, "fastapi==0.141.1"))

    assert "None" in out
    assert "```" not in out


def test_does_not_commit_or_push_the_upgraded_lockfile():
    wf, _ = _load("upgraded-dependencies.yml")
    assert wf["permissions"] == {"contents": "read"}
    assert not any(re.search(r"\bgit (commit|push)\b", r) for r in _runs(wf))


def test_installs_the_same_extras_and_pythons_as_ci():
    canary, _ = _load("upgraded-dependencies.yml")
    ci, _ = _load("ci.yml")

    def extras(runs):
        install = next(r for r in runs if "uv sync" in r)
        return sorted(re.findall(r"--extra (\S+)", install))

    ci_runs = [str(s.get("run", "")) for s in ci["jobs"]["unit-tests"]["steps"]]
    assert extras(_runs(canary)) == extras(ci_runs)

    ci_pythons = sorted(
        job["env"]["PYTHON_VERSION"] for job in ci["jobs"].values() if "PYTHON_VERSION" in job.get("env", {})
    )
    assert sorted(canary["jobs"]["unit-tests"]["strategy"]["matrix"]["python"]) == ci_pythons


def test_pins_setup_uv_to_the_same_commit_as_ci():
    def pin(wf):
        return next(
            s["uses"] for job in wf["jobs"].values() for s in job["steps"]
            if str(s.get("uses", "")).startswith("astral-sh/setup-uv@")
        )

    canary, _ = _load("upgraded-dependencies.yml")
    ci, _ = _load("ci.yml")
    assert pin(canary) == pin(ci)
