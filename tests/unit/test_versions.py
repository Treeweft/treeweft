"""treeweft.versions: the single source of truth for version information (ADR-004 §1)."""
import importlib.metadata
import pathlib
import tomllib

import pytest

from treeweft import versions

_REPO_PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"


def _pyproject(tmp_path, name="treeweft", version="1.2.3"):
    p = tmp_path / "pyproject.toml"
    p.write_text(f'[project]\nname = "{name}"\nversion = "{version}"\n')
    return p


def test_source_version_is_the_repo_pyproject_version():
    with _REPO_PYPROJECT.open("rb") as fh:
        expected = tomllib.load(fh)["project"]["version"]
    assert versions.SOURCE_VERSION == expected
    assert versions.SOURCE_MAJOR == versions.parse_semver(expected)[0]


@pytest.mark.parametrize("good,parsed", [("1.0.0", (1, 0, 0)), ("0.4.12", (0, 4, 12)), ("10.20.30", (10, 20, 30))])
def test_parse_semver_accepts(good, parsed):
    assert versions.parse_semver(good) == parsed


@pytest.mark.parametrize("bad", ["2026.9.23", "1.0", "01.0.0", "1.0.0-rc1", "v1.0.0", "1.0.0.1", ""])
def test_parse_semver_rejects(bad):
    with pytest.raises(ValueError):
        versions.parse_semver(bad)


def test_checkout_pyproject_wins_over_installed_metadata(tmp_path, monkeypatch):
    # An editable install's metadata stays at the version it was installed with;
    # the checkout's pyproject.toml is the truth after a bump.
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.9.0")
    assert versions._read_source_version(_pyproject(tmp_path, version="1.2.3")) == "1.2.3"


def test_calver_era_version_is_named_as_such():
    with pytest.raises(ValueError, match="CalVer-era"):
        versions.parse_semver("2026.9.23")


def test_falls_back_to_metadata_without_a_pyproject(tmp_path, monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "4.5.6")
    assert versions._read_source_version(tmp_path / "missing.toml") == "4.5.6"


def test_ignores_a_foreign_pyproject(tmp_path, monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "4.5.6")
    assert versions._read_source_version(_pyproject(tmp_path, name="other")) == "4.5.6"


def test_fails_loud_when_no_version_source_exists(tmp_path, monkeypatch):
    def _missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _missing)
    with pytest.raises(RuntimeError, match="Cannot determine the Treeweft source version"):
        versions._read_source_version(tmp_path / "missing.toml")


def test_product_release_comes_from_the_image_build(monkeypatch):
    monkeypatch.setenv("TREEWEFT_RELEASE", "2026.10.1")
    assert versions.product_release() == "2026.10.1"


@pytest.mark.parametrize("value", [None, ""])
def test_product_release_is_none_from_source(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("TREEWEFT_RELEASE", raising=False)
    else:
        monkeypatch.setenv("TREEWEFT_RELEASE", value)
    assert versions.product_release() is None
