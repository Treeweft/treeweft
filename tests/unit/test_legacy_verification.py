"""Legacy adoption verification (ADR-004 §3, research R3): index_guard
`_verify_vector_store`, and its wiring into `decide_store` for the graph.

Fakes `treeweft.retriever.sample_chunks` and `treeweft.embedder.embed` —
no real store, no real embedding service.
"""
import asyncio

import httpx
import pytest

from treeweft import embedder, retriever
from treeweft.application import index_guard as ig
from treeweft.domain.index_stamp import ConfiguredIndex, StoreObservation

pytestmark = pytest.mark.asyncio

CFG = ConfiguredIndex(schema=1, embedding_model="Qwen/Qwen3-Embedding-0.6B", vector_dim=4)


def _obs(schema_dim=4):
    return StoreObservation(
        store="vector", backend="fake", exists=True, has_data=True, stamp=None, schema_dim=schema_dim
    )


def _set_sample_chunks(monkeypatch, by_scan_limit: dict):
    """by_scan_limit maps a scan_limit to the samples sample_chunks(n, scan_limit=...)
    should return for that call, simulating a real store skipping over-length rows."""
    calls = []

    async def _fake(n, scan_limit=20):
        calls.append(scan_limit)
        return by_scan_limit.get(scan_limit, [])[:n]

    monkeypatch.setattr(retriever, "sample_chunks", _fake)
    return calls


def _set_embed(monkeypatch, fn):
    """`fn` is a plain sync function `texts -> list[list[float]]`."""
    calls = {"embed": 0, "embed_query": 0}

    async def _embed(texts):
        calls["embed"] += 1
        return fn(texts)

    async def _embed_query(texts):
        calls["embed_query"] += 1
        return fn(texts)

    monkeypatch.setattr(embedder, "embed", _embed)
    monkeypatch.setattr(embedder, "embed_query", _embed_query)
    return calls


class TestDimensionCheckedFirst:
    async def test_dimension_mismatch_fails_before_any_embedding(self, monkeypatch):
        embed_calls = _set_embed(monkeypatch, lambda texts: (_ for _ in ()).throw(AssertionError("must not embed")))
        v = await ig._verify_vector_store(_obs(schema_dim=999), CFG)
        assert v.kind == "failed"
        assert v.check == "dimension"
        assert embed_calls["embed"] == 0


class TestCosineThreshold:
    async def test_all_above_threshold_passes(self, monkeypatch):
        _set_sample_chunks(monkeypatch, {20: [("short a", [1.0, 0.0, 0.0, 0.0]), ("short b", [0.0, 1.0, 0.0, 0.0])]})
        _set_embed(monkeypatch, lambda texts: [[1.0, 0.0, 0.0, 0.0], [0.001, 0.9999, 0.0, 0.0]])
        v = await ig._verify_vector_store(_obs(), CFG)
        assert v.kind == "passed"

    async def test_one_below_threshold_fails_with_the_value_in_detail(self, monkeypatch):
        _set_sample_chunks(monkeypatch, {20: [("short a", [1.0, 0.0, 0.0, 0.0])]})
        # cosine(embedded, stored) well below 0.99
        _set_embed(monkeypatch, lambda texts: [[0.0, 1.0, 0.0, 0.0]])
        v = await ig._verify_vector_store(_obs(), CFG)
        assert v.kind == "failed"
        assert v.check == "embedding_model"
        assert "0.0000" in v.detail or "0.00" in v.detail


class TestEmbedNotEmbedQuery:
    async def test_uses_embed_never_embed_query(self, monkeypatch):
        _set_sample_chunks(monkeypatch, {20: [("short", [1.0, 0.0, 0.0, 0.0])]})
        calls = _set_embed(monkeypatch, lambda texts: [[1.0, 0.0, 0.0, 0.0]])
        await ig._verify_vector_store(_obs(), CFG)
        assert calls["embed"] == 1
        assert calls["embed_query"] == 0


class TestSampleCount:
    async def test_two_rows_is_verified_with_two(self, monkeypatch):
        _set_sample_chunks(monkeypatch, {20: [("a", [1.0, 0.0, 0.0, 0.0]), ("b", [0.0, 1.0, 0.0, 0.0])]})
        _set_embed(monkeypatch, lambda texts: [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
        v = await ig._verify_vector_store(_obs(), CFG)
        assert v.kind == "passed"


class TestNoVerifiableChunks:
    async def test_all_20_long_retries_at_200_then_fails(self, monkeypatch):
        calls = _set_sample_chunks(monkeypatch, {20: [], 200: []})
        v = await ig._verify_vector_store(_obs(), CFG)
        assert calls == [20, 200]
        assert v.kind == "failed"
        assert v.check == "no verifiable chunks"

    async def test_found_at_200_after_empty_at_20(self, monkeypatch):
        _set_sample_chunks(monkeypatch, {20: [], 200: [("short", [1.0, 0.0, 0.0, 0.0])]})
        _set_embed(monkeypatch, lambda texts: [[1.0, 0.0, 0.0, 0.0]])
        v = await ig._verify_vector_store(_obs(), CFG)
        assert v.kind == "passed"


class TestUnavailable:
    async def test_httpx_error_is_unavailable(self, monkeypatch):
        _set_sample_chunks(monkeypatch, {20: [("short", [1.0, 0.0, 0.0, 0.0])]})

        async def _boom(texts):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(embedder, "embed", _boom)
        v = await ig._verify_vector_store(_obs(), CFG)
        assert v.kind == "unavailable"

    async def test_all_backends_unavailable_runtimeerror_is_unavailable(self, monkeypatch):
        _set_sample_chunks(monkeypatch, {20: [("short", [1.0, 0.0, 0.0, 0.0])]})

        async def _boom(texts):
            raise RuntimeError("All embedding backends are unavailable")

        monkeypatch.setattr(embedder, "embed", _boom)
        v = await ig._verify_vector_store(_obs(), CFG)
        assert v.kind == "unavailable"

    async def test_timeout_beyond_configured_limit_is_unavailable(self, monkeypatch):
        monkeypatch.setenv("INDEX_VERIFY_TIMEOUT_SECONDS", "0.01")
        _set_sample_chunks(monkeypatch, {20: [("short", [1.0, 0.0, 0.0, 0.0])]})

        async def _slow(texts):
            await asyncio.sleep(1)
            return [[1.0, 0.0, 0.0, 0.0]]

        monkeypatch.setattr(embedder, "embed", _slow)
        v = await ig._verify_vector_store(_obs(), CFG)
        assert v.kind == "unavailable"

    async def test_sample_chunks_itself_failing_is_unavailable(self, monkeypatch):
        async def _boom(n, scan_limit=20):
            raise RuntimeError("store unreachable")

        monkeypatch.setattr(retriever, "sample_chunks", _boom)
        v = await ig._verify_vector_store(_obs(), CFG)
        assert v.kind == "unavailable"


class TestLogsCosines:
    async def test_info_log_contains_cosines(self, monkeypatch, caplog):
        _set_sample_chunks(monkeypatch, {20: [("short", [1.0, 0.0, 0.0, 0.0])]})
        _set_embed(monkeypatch, lambda texts: [[1.0, 0.0, 0.0, 0.0]])
        with caplog.at_level("INFO", logger="treeweft.application.index_guard"):
            await ig._verify_vector_store(_obs(), CFG)
        assert any("cosines" in r.message for r in caplog.records)


class TestGraphAdoptionIntegration:
    """End-to-end through run_check(): the graph adopts only alongside a
    passing vector verification (data-model.md; unit-level coverage already
    in test_index_stamp_decision.py — this exercises the real wiring)."""

    async def test_graph_adopted_when_vector_verification_passes(self, monkeypatch):
        from treeweft import graph_store

        monkeypatch.setenv("EMBEDDING_MODEL", CFG.embedding_model)
        monkeypatch.setenv("VECTOR_DIM", str(CFG.vector_dim))
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required"))

        async def vs_observe():
            return _obs()

        async def gs_observe():
            return StoreObservation(store="graph", backend="fake-graph", exists=True, has_data=True, stamp=None)

        written = []

        async def gs_write(stamp):
            written.append(stamp)

        async def vs_write(stamp):
            pass

        monkeypatch.setattr(retriever, "observe_index", vs_observe)
        monkeypatch.setattr(retriever, "write_stamp", vs_write)
        monkeypatch.setattr(graph_store, "observe_index", gs_observe)
        monkeypatch.setattr(graph_store, "write_stamp", gs_write)
        _set_sample_chunks(monkeypatch, {20: [("short", [1.0, 0.0, 0.0, 0.0])]})
        _set_embed(monkeypatch, lambda texts: [[1.0, 0.0, 0.0, 0.0]])

        status = await ig.run_check()
        assert status.state == "ok"
        assert len(written) == 1

    async def test_graph_with_data_and_empty_vector_store_is_reindex_required(self, monkeypatch):
        from treeweft import graph_store

        monkeypatch.setenv("EMBEDDING_MODEL", CFG.embedding_model)
        monkeypatch.setenv("VECTOR_DIM", str(CFG.vector_dim))
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required"))

        async def vs_observe():
            return StoreObservation(store="vector", backend="fake", exists=True, has_data=False, stamp=None)

        async def gs_observe():
            return StoreObservation(store="graph", backend="fake-graph", exists=True, has_data=True, stamp=None)

        async def vs_write(stamp):
            pass  # the vector store is legitimately fresh (no data, no stamp)

        async def gs_no_write(stamp):
            raise AssertionError("must not stamp the graph for the reindex_required case")

        monkeypatch.setattr(retriever, "observe_index", vs_observe)
        monkeypatch.setattr(retriever, "write_stamp", vs_write)
        monkeypatch.setattr(graph_store, "observe_index", gs_observe)
        monkeypatch.setattr(graph_store, "write_stamp", gs_no_write)

        status = await ig.run_check()
        assert status.state == "reindex_required"
        assert "graph" in status.reason
