#!/usr/bin/env python3
"""Release tag checks for the Docker publish workflow (ADR-004 §4).

Usage: check_release_tags.py <calver-tag>

1. The tag is CalVer: vYYYY.M.D or vYYYY.M.D.N, no zero padding.
2. The same commit carries the SemVer tag v<pyproject.toml version>.
3. Month rule: an earlier release in the same calendar month must have the
   same SemVer major. Releases whose pyproject version is not SemVer (the
   CalVer era before 1.0.0) are ignored.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

CALVER_TAG = re.compile(
    r"^v(?P<y>\d{4})\.(?P<m>[1-9]|1[0-2])\.(?P<d>[1-9]|[12]\d|3[01])(?:\.(?P<n>[1-9]\d*))?$"
)
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# Treeweft's versions before 1.0.0 were CalVer dates (e.g. 2026.9.23). They are
# SemVer-shaped, so a year-sized major marks a version as CalVer-era, not SemVer
# (same rule as treeweft.versions.parse_semver).
_CALVER_ERA_MAJOR = 1000


def _semver(version: str) -> re.Match | None:
    match = SEMVER.match(version)
    if match is None or int(match[1]) >= _CALVER_ERA_MAJOR:
        return None
    return match


def _git(repo, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo, rev: str) -> str | None:
    try:
        return _git(repo, "rev-list", "-n", "1", rev)
    except subprocess.CalledProcessError:
        return None


def _version_at(repo, rev: str) -> str | None:
    try:
        toml = tomllib.loads(_git(repo, "show", f"{rev}:pyproject.toml"))
        version = toml["project"]["version"]
    except (subprocess.CalledProcessError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return None  # missing/unreadable pyproject.toml, or no parseable version
    return version if isinstance(version, str) else None


def _calver_order(match: re.Match) -> tuple[int, int]:
    # (day, suffix) so no-suffix sorts before .1, .1 before .2, etc.
    return (int(match["d"]), int(match["n"]) if match["n"] else 0)


def check(repo: Path | str, tag: str) -> list[str]:
    calver = CALVER_TAG.match(tag)
    if calver is None:
        return [f"tag {tag!r} is not CalVer (expected vYYYY.M.D or vYYYY.M.D.N, no zero padding)"]
    commit = _commit(repo, tag)
    if commit is None:
        return [f"tag {tag} does not exist"]
    version = _version_at(repo, tag)
    if version is None:
        return [f"pyproject.toml is missing or unreadable at {tag}"]
    semver = _semver(version)
    if semver is None:
        return [f"pyproject.toml version {version!r} at {tag} is not MAJOR.MINOR.PATCH SemVer"]

    errors: list[str] = []
    semver_tag = f"v{version}"
    semver_commit = _commit(repo, semver_tag)
    if semver_commit != commit:
        state = "is missing" if semver_commit is None else "points elsewhere"
        errors.append(f"{tag} must be on the same commit as the SemVer tag {semver_tag}, "
                      f"which {state}; push {semver_tag} first")

    major = int(semver[1])
    this_order = _calver_order(calver)
    for other in _git(repo, "tag", "--list", f"v{calver['y']}.{calver['m']}.*").split():
        if other == tag:
            continue
        other_calver = CALVER_TAG.match(other)
        if other_calver is None:
            continue
        if _calver_order(other_calver) >= this_order:
            continue  # only releases earlier in CalVer order count, regardless of git ancestry
        other_version = _version_at(repo, other)
        if other_version is None:
            print(f"::warning::pyproject.toml is missing or unreadable at {other}; "
                  "the month rule was not checked against it")
            continue
        other_semver = _semver(other_version)
        if other_semver is None:
            continue  # CalVer-era release: the month rule starts with SemVer
        if int(other_semver[1]) != major:
            errors.append(
                f"breaking release mid-month: {other} shipped {other_semver[0]}, this release is "
                f"{version} (major {major}); publish it as the first release of next month"
            )
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_release_tags.py <calver-tag>", file=sys.stderr)
        return 2
    errors = check(Path.cwd(), argv[1])
    for error in errors:
        print(f"::error::{error}")
    if not errors:
        print(f"ok: {argv[1]} is CalVer, carries its SemVer tag, and respects the month rule")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
