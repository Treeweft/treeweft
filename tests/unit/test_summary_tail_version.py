"""Unit tests for the summary tail's default read version (research R8, T014).

Covers `_resolve_tail_summaries`, the seam retrieval.py's summary_tail path
calls to look up cached summaries for the tail chunks:

- With no per-request version, each tail chunk is read at its source's
  RECORDED version (`source_records.summary_prompt_version`), falling back to
  the source's EFFECTIVE version (`prompt_pins.effective`) for a chunk MISSED
  entirely at the recorded version, and when the recorded version is unknown
  (missing source record). A cached REJECTION at the recorded version (the ""
  marker) is a resolved outcome, not a miss, and must NOT fall through to the
  effective version. Chunks are grouped by version, so there is one
  `cache_get_many` call per distinct version.
- An explicit per-request `summary_prompt_version` is passed through
  unchanged and unvalidated, in a single `cache_get_many` call.
- The result is keyed by (source_id, sha1), not sha1 alone: two sources can
  share a byte-identical chunk (same sha1) while recorded at different
  chunk_summary versions, and each tail entry must resolve to its OWN
  source's summary.
"""
import pytest

from treeweft.application import indexer_state as _state
from treeweft.application import prompt_pins
from treeweft.application.retrieval import _resolve_tail_summaries
from treeweft.llm import chunk_cache_key


class _FakeCacheGetMany:
    """Records every call and serves from a per-version rows table."""

    def __init__(self, rows_by_version: dict[int, dict[str, str]]):
        self.rows_by_version = rows_by_version
        self.calls: list[tuple[tuple[str, ...], int, bool]] = []

    async def __call__(self, sha1s, prompt_version, include_rejected=False):
        self.calls.append((tuple(sha1s), prompt_version, include_rejected))
        rows = self.rows_by_version.get(prompt_version, {})
        return {sha: rows[sha] for sha in sha1s if sha in rows}


class _FakeSourceRepo:
    def __init__(self, records: dict[str, int | None]):
        self.records = records

    async def summary_versions_for(self, source_ids):
        return {
            source_id: self.records[source_id]
            for source_id in source_ids
            if source_id in self.records
        }


@pytest.fixture
def patched_llm(monkeypatch):
    def _install(rows_by_version):
        fake = _FakeCacheGetMany(rows_by_version)
        monkeypatch.setattr(
            "treeweft.application.retrieval.llm.cache_get_many", fake
        )
        return fake

    return _install


@pytest.fixture
def patched_source_repo(monkeypatch):
    def _install(records):
        monkeypatch.setattr(_state, "_source_repo", _FakeSourceRepo(records))

    return _install


@pytest.fixture
def patched_effective(monkeypatch):
    def _install(versions_by_source: dict[str | None, int]):
        def _effective(operation, source_id=None):
            assert operation == "chunk_summary"
            return versions_by_source[source_id]

        monkeypatch.setattr(prompt_pins, "effective", _effective)

    return _install


def _calls_without_flag(fake_llm):
    return [(shas, version) for shas, version, _ in fake_llm.calls]


@pytest.mark.asyncio
async def test_recorded_version_used_when_present(
    patched_llm, patched_source_repo, patched_effective,
):
    sha_a = chunk_cache_key("chunk a")
    fake_llm = patched_llm({5: {sha_a: "summary at v5"}})
    patched_source_repo({"src-1": 5})
    patched_effective({"src-1": 9})

    out = await _resolve_tail_summaries(
        [(sha_a, "src-1")], summary_prompt_version=None,
    )

    assert out == {("src-1", sha_a): "summary at v5"}
    assert _calls_without_flag(fake_llm) == [((sha_a,), 5)]
    # Recorded-version reads ask for rejections too, so a cached rejection is
    # honoured rather than silently treated as a miss.
    assert fake_llm.calls[0][2] is True


@pytest.mark.asyncio
async def test_miss_at_recorded_version_falls_back_to_effective(
    patched_llm, patched_source_repo, patched_effective,
):
    sha_a = chunk_cache_key("chunk a")
    fake_llm = patched_llm({9: {sha_a: "summary at v9"}})
    patched_source_repo({"src-1": 5})
    patched_effective({"src-1": 9})

    out = await _resolve_tail_summaries(
        [(sha_a, "src-1")], summary_prompt_version=None,
    )

    assert out == {("src-1", sha_a): "summary at v9"}
    assert _calls_without_flag(fake_llm) == [((sha_a,), 5), ((sha_a,), 9)]


@pytest.mark.asyncio
async def test_rejected_summary_at_recorded_version_is_honoured_not_effective(
    patched_llm, patched_source_repo, patched_effective,
):
    sha_a = chunk_cache_key("chunk a")
    # "" is the REJECTED_SUMMARY marker: a cached rejection at the recorded
    # version is a resolved outcome ("no summary for this chunk"), not a
    # miss, so it must not fall through to a re-read at the effective
    # version -- doing so could silently resurrect a different answer.
    fake_llm = patched_llm({5: {sha_a: ""}, 9: {sha_a: "summary at v9"}})
    patched_source_repo({"src-1": 5})
    patched_effective({"src-1": 9})

    out = await _resolve_tail_summaries(
        [(sha_a, "src-1")], summary_prompt_version=None,
    )

    assert out == {("src-1", sha_a): ""}
    # Only the recorded-version call happened -- no effective-version fallback.
    assert _calls_without_flag(fake_llm) == [((sha_a,), 5)]


@pytest.mark.asyncio
async def test_missing_source_record_falls_back_to_effective_directly(
    patched_llm, patched_source_repo, patched_effective,
):
    sha_a = chunk_cache_key("chunk a")
    fake_llm = patched_llm({9: {sha_a: "summary at v9"}})
    patched_source_repo({})  # unknown source -> unknown recorded version
    patched_effective({"src-1": 9})

    out = await _resolve_tail_summaries(
        [(sha_a, "src-1")], summary_prompt_version=None,
    )

    assert out == {("src-1", sha_a): "summary at v9"}
    # No recorded version -> no wasted call at an unknown version.
    assert _calls_without_flag(fake_llm) == [((sha_a,), 9)]


@pytest.mark.asyncio
async def test_groups_calls_by_distinct_version(
    patched_llm, patched_source_repo, patched_effective,
):
    sha_a = chunk_cache_key("chunk a")
    sha_b = chunk_cache_key("chunk b")
    sha_c = chunk_cache_key("chunk c")
    fake_llm = patched_llm({
        5: {sha_a: "a@5", sha_b: "b@5"},
        7: {sha_c: "c@7"},
    })
    patched_source_repo({"src-1": 5, "src-2": 5, "src-3": 7})
    patched_effective({"src-1": 9, "src-2": 9, "src-3": 9})

    out = await _resolve_tail_summaries(
        [(sha_a, "src-1"), (sha_b, "src-2"), (sha_c, "src-3")],
        summary_prompt_version=None,
    )

    assert out == {
        ("src-1", sha_a): "a@5",
        ("src-2", sha_b): "b@5",
        ("src-3", sha_c): "c@7",
    }
    # One call per distinct recorded version, all hits -> no effective fallback call.
    calls = _calls_without_flag(fake_llm)
    assert len(calls) == 2
    calls_by_version = {version: set(shas) for shas, version in calls}
    assert calls_by_version == {5: {sha_a, sha_b}, 7: {sha_c}}


@pytest.mark.asyncio
async def test_duplicate_sha_across_sources_each_gets_its_own_recorded_version(
    patched_llm, patched_source_repo, patched_effective,
):
    # Two sources index a byte-identical chunk (same sha1) but were
    # summarized at different recorded chunk_summary versions -- each tail
    # entry must resolve to ITS OWN source's summary, not whichever source's
    # cache_get_many call happened to run last.
    sha_shared = chunk_cache_key("shared chunk body")
    fake_llm = patched_llm({
        5: {sha_shared: "summary at v5"},
        7: {sha_shared: "summary at v7"},
    })
    patched_source_repo({"src-1": 5, "src-2": 7})
    patched_effective({"src-1": 9, "src-2": 9})

    out = await _resolve_tail_summaries(
        [(sha_shared, "src-1"), (sha_shared, "src-2")],
        summary_prompt_version=None,
    )

    assert out == {
        ("src-1", sha_shared): "summary at v5",
        ("src-2", sha_shared): "summary at v7",
    }


@pytest.mark.asyncio
async def test_explicit_version_passed_through_unvalidated_single_call(
    patched_llm, patched_source_repo, monkeypatch,
):
    sha_a = chunk_cache_key("chunk a")
    # 9002 is deliberately unregistered (the benchmark's read override).
    fake_llm = patched_llm({9002: {sha_a: "summary at v9002"}})
    patched_source_repo({"src-1": 5})

    def _effective_should_not_be_called(operation, source_id=None):
        raise AssertionError("effective() must not be consulted with an explicit version")

    monkeypatch.setattr(prompt_pins, "effective", _effective_should_not_be_called)

    out = await _resolve_tail_summaries(
        [(sha_a, "src-1")], summary_prompt_version=9002,
    )

    assert out == {("src-1", sha_a): "summary at v9002"}
    assert _calls_without_flag(fake_llm) == [((sha_a,), 9002)]


@pytest.mark.asyncio
async def test_no_tail_entries_returns_empty_without_calling_cache(
    patched_llm, patched_source_repo, patched_effective,
):
    fake_llm = patched_llm({})
    patched_source_repo({})
    patched_effective({})

    out = await _resolve_tail_summaries([], summary_prompt_version=None)

    assert out == {}
    assert fake_llm.calls == []
