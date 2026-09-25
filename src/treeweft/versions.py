"""Treeweft version information (ADR-004 §1).

SOURCE_VERSION is the SemVer of the source tree. It is read from the
pyproject.toml beside the source tree when present (a checkout, or an image
that copies it), and otherwise from installed package metadata. The checkout
wins so an editable install reports a version bump immediately; its metadata
keeps the version it was installed with until it is reinstalled.

product_release() is the CalVer product release. Published images bake it in
through the TREEWEFT_RELEASE build argument; from a source checkout it is None,
because nothing outside a published image knows which product release it is.
"""
from __future__ import annotations

import importlib.metadata
import os
import re
import tomllib
from pathlib import Path

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# src/treeweft/versions.py -> parents[2] is the repository (or image) root.
_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

# Treeweft's versions before 1.0.0 were CalVer dates (e.g. 2026.9.23). They are
# SemVer-shaped, so a year-sized major marks a version as CalVer-era, not SemVer.
_CALVER_ERA_MAJOR = 1000


def parse_semver(version: str) -> tuple[int, int, int]:
    match = SEMVER.match(version)
    if match is None:
        raise ValueError(f"{version!r} is not MAJOR.MINOR.PATCH SemVer")
    major = int(match[1])
    if major >= _CALVER_ERA_MAJOR:
        raise ValueError(f"{version!r} is a CalVer-era Treeweft version, not SemVer")
    return major, int(match[2]), int(match[3])


def _read_source_version(pyproject: Path = _PYPROJECT) -> str:
    if pyproject.is_file():
        with pyproject.open("rb") as fh:
            project = tomllib.load(fh).get("project", {})
        if project.get("name") == "treeweft":
            return project["version"]
    try:
        return importlib.metadata.version("treeweft")
    except importlib.metadata.PackageNotFoundError:
        raise RuntimeError(
            "Cannot determine the Treeweft source version: no treeweft "
            f"pyproject.toml at {pyproject} and the package is not installed."
        ) from None


def product_release() -> str | None:
    return os.environ.get("TREEWEFT_RELEASE") or None


SOURCE_VERSION = _read_source_version()
SOURCE_MAJOR = parse_semver(SOURCE_VERSION)[0]
