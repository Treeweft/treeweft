"""Treeweft's source version is SemVer (ADR-004).

- `pyproject.toml` carries MAJOR.MINOR.PATCH, starting at 1.0.0;
- product releases (git tag vYYYY.M.D, image tags) are CalVer and live only
  in tags — see scripts/check_release_tags.py and docs/docker-images.md.
"""
import pathlib
import tomllib

from treeweft.versions import parse_semver

_PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"


def _project_version() -> str:
    with _PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_pyproject_version_is_semver():
    version = _project_version()
    parse_semver(version)  # raises ValueError for non-SemVer and CalVer-era versions


def test_semver_is_at_least_1_0_0():
    major = int(_project_version().split(".")[0])
    assert major >= 1, "ADR-004: the first SemVer release is 1.0.0"
