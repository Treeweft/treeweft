"""The pure index-stamp decision table (ADR-004 §3, data-model.md).

domain/index_stamp.py has no adapter imports — every case here is exercised
with plain values, no stores, no I/O.
"""

import pytest

from treeweft.domain.index_stamp import (
    ConfiguredIndex,
    IndexStamp,
    RebuildState,
    StoreObservation,
    UnreadableStamp,
    Verification,
    aggregate,
    decide_store,
    format_reason,
    parse_stamp,
)

CONFIGURED = ConfiguredIndex(schema=1, embedding_model="Qwen/Qwen3-Embedding-0.6B", vector_dim=1024)
MATCHING_STAMP = IndexStamp(schema=1, embedding_model="Qwen/Qwen3-Embedding-0.6B", vector_dim=1024)


def _obs(store="vector", backend="milvus", exists=True, has_data=True, stamp=None,
         schema_dim=None, unreachable=None):
    return StoreObservation(
        store=store, backend=backend, exists=exists, has_data=has_data,
        stamp=stamp, schema_dim=schema_dim, unreachable=unreachable,
    )


class TestDecideStore:
    def test_unreachable_is_unverified(self):
        check = decide_store(_obs(unreachable="connection refused"), CONFIGURED, Verification.not_run())
        assert check.state == "unverified"
        assert "unreachable" in check.reason
        assert "connection refused" in check.reason

    def test_vector_collection_absent_is_ok_with_no_write(self):
        check = decide_store(_obs(exists=False, has_data=False), CONFIGURED, Verification.not_run())
        assert check.state == "ok"
        assert check.write_stamp is False

    def test_no_data_no_stamp_is_ok_and_writes_stamp(self):
        check = decide_store(_obs(has_data=False, stamp=None), CONFIGURED, Verification.not_run())
        assert check.state == "ok"
        assert check.write_stamp is True

    def test_matching_stamp_is_ok(self):
        check = decide_store(_obs(stamp=MATCHING_STAMP), CONFIGURED, Verification.not_run())
        assert check.state == "ok"
        assert check.write_stamp is False

    @pytest.mark.parametrize(
        "field,stamp",
        [
            ("schema", IndexStamp(schema=2, embedding_model="Qwen/Qwen3-Embedding-0.6B", vector_dim=1024)),
            ("embedding_model", IndexStamp(schema=1, embedding_model="BAAI/bge-m3", vector_dim=1024)),
            ("vector_dim", IndexStamp(schema=1, embedding_model="Qwen/Qwen3-Embedding-0.6B", vector_dim=768)),
        ],
    )
    def test_each_field_mismatching_is_reindex_required(self, field, stamp):
        check = decide_store(_obs(stamp=stamp), CONFIGURED, Verification.not_run())
        assert check.state == "reindex_required"
        assert field in check.reason

    def test_stored_schema_newer_than_current_is_reindex_required(self):
        newer = IndexStamp(schema=99, embedding_model="Qwen/Qwen3-Embedding-0.6B", vector_dim=1024)
        check = decide_store(_obs(stamp=newer), CONFIGURED, Verification.not_run())
        assert check.state == "reindex_required"
        assert "schema" in check.reason

    def test_unreadable_stamp_is_reindex_required_never_treated_as_no_stamp(self):
        check = decide_store(_obs(stamp=UnreadableStamp("missing key: embedding_model")), CONFIGURED, Verification.not_run())
        assert check.state == "reindex_required"
        assert check.write_stamp is False
        assert "unreadable" in check.reason

    def test_legacy_verification_passed_adopts_and_writes_stamp(self):
        check = decide_store(_obs(stamp=None, has_data=True), CONFIGURED, Verification.passed(), vector_has_data=True)
        assert check.state == "ok"
        assert check.write_stamp is True

    def test_legacy_verification_failed_is_reindex_required_naming_check(self):
        v = Verification.failed("embedding_model", "cosine 0.87 < 0.99")
        check = decide_store(_obs(stamp=None, has_data=True), CONFIGURED, v, vector_has_data=True)
        assert check.state == "reindex_required"
        assert "embedding_model" in check.reason
        assert "0.87" in check.reason

    def test_legacy_verification_unavailable_is_unverified(self):
        v = Verification.unavailable("embedding service unreachable")
        check = decide_store(_obs(stamp=None, has_data=True), CONFIGURED, v, vector_has_data=True)
        assert check.state == "unverified"
        assert check.write_stamp is False

    def test_legacy_verification_not_run_is_unverified_never_adopts(self):
        check = decide_store(_obs(stamp=None, has_data=True), CONFIGURED, Verification.not_run(), vector_has_data=True)
        assert check.state == "unverified"
        assert check.write_stamp is False

    def test_graph_with_data_no_stamp_and_empty_vector_store_is_reindex_required(self):
        check = decide_store(
            _obs(store="graph", backend="neo4j", stamp=None, has_data=True),
            CONFIGURED, Verification.passed(), vector_has_data=False,
        )
        assert check.state == "reindex_required"
        assert "graph" in check.reason
        assert "vector" in check.reason

    def test_graph_adopted_only_when_vector_verification_passed(self):
        check = decide_store(
            _obs(store="graph", backend="neo4j", stamp=None, has_data=True),
            CONFIGURED, Verification.passed(), vector_has_data=True,
        )
        assert check.state == "ok"
        assert check.write_stamp is True

    def test_reason_format_exact(self):
        assert format_reason("vector", "milvus", "embedding_model", "BAAI/bge-m3", "Qwen/Qwen3-Embedding-0.6B") == (
            "vector store (milvus): embedding_model is BAAI/bge-m3, configured Qwen/Qwen3-Embedding-0.6B"
        )

    def test_several_reasons_joined_with_semicolon(self):
        stamp = IndexStamp(schema=2, embedding_model="BAAI/bge-m3", vector_dim=1024)
        check = decide_store(_obs(stamp=stamp), CONFIGURED, Verification.not_run())
        assert "; " in check.reason
        assert check.reason.count(";") == 1


class TestParseStamp:
    def test_none_is_no_stamp(self):
        assert parse_stamp(None) is None

    def test_empty_is_no_stamp(self):
        assert parse_stamp({}) is None

    def test_valid_string_keyed_stamp_parses(self):
        stamp = parse_stamp({"index_schema": "1", "embedding_model": "x", "vector_dim": "1024"})
        assert stamp == IndexStamp(schema=1, embedding_model="x", vector_dim=1024)

    def test_valid_native_typed_stamp_parses(self):
        stamp = parse_stamp({"index_schema": 1, "embedding_model": "x", "vector_dim": 1024})
        assert stamp == IndexStamp(schema=1, embedding_model="x", vector_dim=1024)

    def test_non_integer_schema_is_unreadable(self):
        stamp = parse_stamp({"index_schema": "abc", "embedding_model": "x", "vector_dim": "1024"})
        assert isinstance(stamp, UnreadableStamp)

    def test_missing_key_is_unreadable(self):
        stamp = parse_stamp({"index_schema": "1", "vector_dim": "1024"})
        assert isinstance(stamp, UnreadableStamp)

    def test_empty_embedding_model_is_unreadable(self):
        stamp = parse_stamp({"index_schema": "1", "embedding_model": "", "vector_dim": "1024"})
        assert isinstance(stamp, UnreadableStamp)


class TestAggregate:
    def test_all_ok_is_ok(self):
        checks = [
            decide_store(_obs(stamp=MATCHING_STAMP), CONFIGURED, Verification.not_run()),
            decide_store(_obs(store="graph", backend="sqlite", stamp=MATCHING_STAMP), CONFIGURED, Verification.not_run()),
        ]
        status = aggregate(checks, RebuildState("none"))
        assert status.state == "ok"

    def test_reindex_required_beats_unverified(self):
        checks = [
            decide_store(_obs(unreachable="x"), CONFIGURED, Verification.not_run()),  # unverified
            decide_store(_obs(store="graph", stamp=IndexStamp(2, "x", 1)), CONFIGURED, Verification.not_run()),  # reindex_required
        ]
        status = aggregate(checks, RebuildState("none"))
        assert status.state == "reindex_required"

    def test_unverified_beats_rebuilding(self):
        checks = [decide_store(_obs(unreachable="x"), CONFIGURED, Verification.not_run())]
        status = aggregate(checks, RebuildState("rebuilding", done=1, total=3))
        assert status.state == "unverified"

    def test_rebuilding_beats_ok(self):
        checks = [decide_store(_obs(stamp=MATCHING_STAMP), CONFIGURED, Verification.not_run())]
        status = aggregate(checks, RebuildState("rebuilding", done=2, total=5))
        assert status.state == "rebuilding"
        assert status.rebuild_progress == (2, 5)

    def test_preparing_beats_every_store_check(self):
        checks = [decide_store(_obs(unreachable="x"), CONFIGURED, Verification.not_run())]
        status = aggregate(checks, RebuildState("preparing", total=4))
        assert status.state == "rebuilding"
        assert status.preparing is True
        assert status.rebuild_progress == (0, 4)

    def test_interrupted_is_reindex_required_with_fixed_reason(self):
        checks = [decide_store(_obs(stamp=MATCHING_STAMP), CONFIGURED, Verification.not_run())]
        status = aggregate(checks, RebuildState("interrupted"))
        assert status.state == "reindex_required"
        assert status.reason == "a rebuild was interrupted; run it again"

    def test_interrupted_beats_ok_store_checks(self):
        checks = [decide_store(_obs(stamp=MATCHING_STAMP), CONFIGURED, Verification.not_run())]
        status = aggregate(checks, RebuildState("interrupted"))
        assert status.state == "reindex_required"

    def test_complete_defers_to_store_checks(self):
        checks = [decide_store(_obs(stamp=MATCHING_STAMP), CONFIGURED, Verification.not_run())]
        status = aggregate(checks, RebuildState("complete"))
        assert status.state == "ok"

    def test_default_rebuild_state_is_none_and_behaves_like_ok(self):
        checks = [decide_store(_obs(stamp=MATCHING_STAMP), CONFIGURED, Verification.not_run())]
        status = aggregate(checks)
        assert status.state == "ok"
