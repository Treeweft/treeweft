"""The weekly canary tests upgraded dependencies and never gates a merge (#13)."""
import pathlib
import re

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


def test_names_the_upgraded_packages_in_the_run_summary():
    wf, _ = _load("upgraded-dependencies.yml")
    upgrade = next(r for r in _runs(wf) if "uv lock --upgrade" in r)
    assert "GITHUB_STEP_SUMMARY" in upgrade


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
