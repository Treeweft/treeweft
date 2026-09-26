"""Tests for the prompt-pins domain helpers (ADR-003 prompt versioning).

Pure domain logic: resolving the effective prompt version for an operation,
deciding whether a source's stored summary vectors are stale, and seeding
a deployment pin at startup. No I/O, no adapter imports.
"""

import pytest

from treeweft.domain.prompt_pins import PinView, is_stale, resolve, seed_version


class TestResolve:
    def test_override_beats_deployment_pin(self):
        view = PinView(
            deployment={"chunk_summary": 3, "hyde": 1},
            overrides={"src-1": 5},
        )

        assert resolve(view, "chunk_summary", "src-1") == 5

    def test_deployment_pin_applies_without_override(self):
        view = PinView(
            deployment={"chunk_summary": 3, "hyde": 1},
            overrides={"src-1": 5},
        )

        assert resolve(view, "chunk_summary", "src-2") == 3

    def test_deployment_pin_applies_with_no_source_id(self):
        view = PinView(deployment={"chunk_summary": 3, "hyde": 1}, overrides={})

        assert resolve(view, "chunk_summary") == 3

    def test_hyde_returns_deployment_pin(self):
        view = PinView(deployment={"chunk_summary": 3, "hyde": 1}, overrides={})

        assert resolve(view, "hyde") == 1

    def test_hyde_ignores_source_id(self):
        view = PinView(
            deployment={"chunk_summary": 3, "hyde": 1},
            overrides={"src-1": 5},
        )

        assert resolve(view, "hyde", "src-1") == 1


class TestIsStale:
    def test_true_when_refresh_target_set_even_if_recorded_equals_effective(self):
        assert is_stale(recorded=3, refresh_target=3, effective=3) is True

    def test_true_when_recorded_known_and_differs_from_effective(self):
        assert is_stale(recorded=2, refresh_target=None, effective=3) is True

    def test_false_when_recorded_none_and_no_target(self):
        assert is_stale(recorded=None, refresh_target=None, effective=3) is False

    def test_false_when_recorded_equals_effective_and_no_target(self):
        assert is_stale(recorded=3, refresh_target=None, effective=3) is False


class TestSeedVersion:
    def test_no_sources_seeds_latest(self):
        assert (
            seed_version(
                "chunk_summary", histogram={}, has_sources=False, latest=3
            )
            == 3
        )

    def test_sources_with_histogram_seeds_most_common_version(self):
        histogram = {1: 2, 3: 5, 2: 1}

        assert (
            seed_version(
                "chunk_summary", histogram=histogram, has_sources=True, latest=3
            )
            == 3
        )

    def test_ties_go_to_higher_version(self):
        histogram = {2: 4, 3: 4}

        assert (
            seed_version(
                "chunk_summary", histogram=histogram, has_sources=True, latest=3
            )
            == 3
        )

    def test_sources_but_empty_histogram_seeds_latest(self):
        assert (
            seed_version(
                "chunk_summary", histogram={}, has_sources=True, latest=3
            )
            == 3
        )

    def test_hyde_with_sources_seeds_one(self):
        assert (
            seed_version("hyde", histogram={}, has_sources=True, latest=7) == 1
        )

    def test_hyde_without_sources_seeds_latest(self):
        assert (
            seed_version("hyde", histogram={}, has_sources=False, latest=7) == 7
        )


class TestPinViewFrozen:
    def test_pin_view_is_frozen(self):
        view = PinView(deployment={"hyde": 1}, overrides={})

        with pytest.raises(Exception):
            view.deployment = {"hyde": 2}
