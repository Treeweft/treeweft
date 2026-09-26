"""Prompt-pin resolution domain model (ADR-003 prompt versioning).

Pure decision logic for resolving the effective prompt version per operation,
deciding whether a source's stored summary vectors are stale, and seeding a
deployment pin at startup. No I/O, no adapter imports (constitution IV): the
registry of valid versions and the "latest" version for an operation live in
adapters and are passed in as plain values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class PinView:
    """Immutable snapshot of the current prompt pins.

    ``deployment`` maps operation name (``"chunk_summary"``, ``"hyde"``) to
    its deployment-wide pinned version. ``overrides`` maps source ID to a
    per-source override of the ``chunk_summary`` version; HyDE has no
    per-source overrides.
    """

    deployment: Mapping[str, int]
    overrides: Mapping[str, int]


def resolve(view: PinView, operation: str, source_id: Optional[str] = None) -> int:
    """Return the effective prompt version for ``operation``.

    HyDE always resolves to its deployment pin; any ``source_id`` passed for
    HyDE is ignored (FR-005). Any other operation (``chunk_summary``)
    resolves to the source's override when one is set for ``source_id``,
    otherwise to the deployment pin.
    """

    if operation == "hyde":
        return view.deployment["hyde"]

    if source_id is not None and source_id in view.overrides:
        return view.overrides[source_id]

    return view.deployment[operation]


def is_stale(
    recorded: Optional[int],
    refresh_target: Optional[int],
    effective: int,
) -> bool:
    """Return whether a source's stored summary vectors are stale.

    True whenever a refresh toward ``refresh_target`` is unfinished, even if
    ``recorded`` already equals ``effective`` (FR-015: mixed vectors during a
    refresh are never reported current). Otherwise true when ``recorded`` is
    known and differs from ``effective``. A source that has never been
    summarized (``recorded is None``) and has no refresh in flight is never
    stale.
    """

    if refresh_target is not None:
        return True

    if recorded is not None and recorded != effective:
        return True

    return False


def seed_version(
    operation: str,
    histogram: Mapping[int, int],
    has_sources: bool,
    latest: int,
) -> int:
    """Return the version to seed a missing deployment pin for ``operation``.

    ``histogram`` maps a recorded ``summary_prompt_version`` to how many
    sources carry it (NULL/unknown versions are not counted). With no
    sources, or with sources but nothing recorded, the seed is ``latest``.
    With sources, ``chunk_summary`` seeds to the most common recorded
    version, ties going to the higher version; ``hyde`` always seeds to 1
    when there are sources, matching the version summaries were produced
    under before this feature existed (FR-013).
    """

    if not has_sources:
        return latest

    if operation == "hyde":
        return 1

    if not histogram:
        return latest

    best_version = None
    best_count = -1
    for version, count in histogram.items():
        if count > best_count or (count == best_count and version > best_version):
            best_version = version
            best_count = count

    return best_version
