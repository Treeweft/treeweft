"""index_guard: the check, /health fields, and the refresh loop (ADR-004 §3).

Fakes the vector and graph store shims by monkeypatching attributes on the
`treeweft.retriever` / `treeweft.graph_store` module objects that
index_guard imports — index_guard calls through those same objects, so the
patch is visible to it. No real store, no real Postgres.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from treeweft import embedder, graph_store, retriever
from treeweft.adapters.postgresql import maintenance_lock
from treeweft.application import index_guard as ig
from treeweft.application.indexer_service import app
from treeweft.domain.index_stamp import IndexStamp, StoreObservation

pytestmark = pytest.mark.asyncio

MODEL = "Qwen/Qwen3-Embedding-0.6B"
DIM = 1024
MATCHING_STAMP = IndexStamp(schema=1, embedding_model=MODEL, vector_dim=DIM)


async def _one_short_sample(dim=DIM):
    return [("short chunk", [1.0] + [0.0] * (dim - 1))]


class FakeVectorStore:
    """A fake behind `treeweft.retriever`."""

    def __init__(self, exists=True, has_data=False, stamp=None, schema_dim=None, unreachable=None):
        self.exists = exists
        self.has_data = has_data
        self.stamp = stamp
        self.schema_dim = schema_dim
        self.unreachable = unreachable
        self.written_stamps: list[IndexStamp] = []
        self.observe_calls = 0

    async def observe_index(self) -> StoreObservation:
        self.observe_calls += 1
        return StoreObservation(
            store="vector", backend="fake-vector", exists=self.exists, has_data=self.has_data,
            stamp=self.stamp, schema_dim=self.schema_dim, unreachable=self.unreachable,
        )

    async def write_stamp(self, stamp: IndexStamp) -> None:
        self.written_stamps.append(stamp)
        self.stamp = stamp

    async def sample_chunks(self, n, scan_limit=20):
        return []


class FakeGraphStore:
    """A fake behind `treeweft.graph_store`."""

    def __init__(self, exists=True, has_data=False, stamp=None, unreachable=None):
        self.exists = exists
        self.has_data = has_data
        self.stamp = stamp
        self.unreachable = unreachable
        self.written_stamps: list[IndexStamp] = []
        self.observe_calls = 0

    async def observe_index(self) -> StoreObservation:
        self.observe_calls += 1
        return StoreObservation(
            store="graph", backend="fake-graph", exists=self.exists, has_data=self.has_data,
            stamp=self.stamp, unreachable=self.unreachable,
        )

    async def write_stamp(self, stamp: IndexStamp) -> None:
        self.written_stamps.append(stamp)
        self.stamp = stamp


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", MODEL)
    monkeypatch.setenv("VECTOR_DIM", str(DIM))
    monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required", reason="not yet checked"))
    monkeypatch.setattr(ig, "_refreshed_at", None)
    # No Postgres in these tests: the lock/rebuild-state paths degrade to
    # their documented no-op / "none" behaviour (research R13).
    monkeypatch.setattr(maintenance_lock, "get_pool", _none_pool)
    yield


async def _none_pool():
    return None


def _install(vs: FakeVectorStore, gs: FakeGraphStore, monkeypatch):
    monkeypatch.setattr(retriever, "observe_index", vs.observe_index)
    monkeypatch.setattr(retriever, "write_stamp", vs.write_stamp)
    monkeypatch.setattr(retriever, "sample_chunks", vs.sample_chunks)
    monkeypatch.setattr(graph_store, "observe_index", gs.observe_index)
    monkeypatch.setattr(graph_store, "write_stamp", gs.write_stamp)


class TestRunCheck:
    async def test_fresh_stores_stamp_the_graph_and_report_ok(self, monkeypatch):
        vs = FakeVectorStore(exists=False, has_data=False, stamp=None)
        gs = FakeGraphStore(exists=True, has_data=False, stamp=None)
        _install(vs, gs, monkeypatch)
        status = await ig.run_check()
        assert status.state == "ok"
        assert gs.written_stamps == [MATCHING_STAMP]
        # The vector "collection absent" case writes nothing — init_collection
        # stamps it when it's created (research R2).
        assert vs.written_stamps == []

    async def test_matching_stamps_are_ok_and_nothing_is_written(self, monkeypatch):
        vs = FakeVectorStore(exists=True, has_data=True, stamp=MATCHING_STAMP, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=MATCHING_STAMP)
        _install(vs, gs, monkeypatch)
        status = await ig.run_check()
        assert status.state == "ok"
        assert vs.written_stamps == []
        assert gs.written_stamps == []

    async def test_model_mismatch_is_reindex_required_with_exact_reason(self, monkeypatch):
        mismatched = IndexStamp(schema=1, embedding_model="BAAI/bge-m3", vector_dim=DIM)
        vs = FakeVectorStore(exists=True, has_data=True, stamp=mismatched, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=MATCHING_STAMP)
        _install(vs, gs, monkeypatch)
        status = await ig.run_check()
        assert status.state == "reindex_required"
        assert status.reason == (
            f"vector store (fake-vector): embedding_model is BAAI/bge-m3, configured {MODEL}"
        )

    async def test_both_stores_mismatched_reports_both_reasons(self, monkeypatch):
        v_bad = IndexStamp(schema=1, embedding_model="BAAI/bge-m3", vector_dim=DIM)
        g_bad = IndexStamp(schema=2, embedding_model=MODEL, vector_dim=DIM)
        vs = FakeVectorStore(exists=True, has_data=True, stamp=v_bad, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=g_bad)
        _install(vs, gs, monkeypatch)
        status = await ig.run_check()
        assert status.state == "reindex_required"
        assert "fake-vector" in status.reason
        assert "fake-graph" in status.reason


class TestHealthFields:
    async def test_health_route_reports_fields_and_status_stays_ok(self, monkeypatch):
        vs = FakeVectorStore(exists=True, has_data=True, stamp=MATCHING_STAMP, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=MATCHING_STAMP)
        _install(vs, gs, monkeypatch)
        await ig.run_check()
        client = TestClient(app)
        resp = client.get("/health")
        body = resp.json()
        assert body["status"] == "ok"
        assert body["index_schema"] == 1
        assert body["index_status"] == "ok"
        assert "reindex_reason" not in body

    async def test_health_shows_reindex_reason_when_mismatched(self, monkeypatch):
        mismatched = IndexStamp(schema=1, embedding_model="BAAI/bge-m3", vector_dim=DIM)
        vs = FakeVectorStore(exists=True, has_data=True, stamp=mismatched, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=MATCHING_STAMP)
        _install(vs, gs, monkeypatch)
        await ig.run_check()
        client = TestClient(app)
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["index_status"] == "reindex_required"
        assert "reindex_reason" in body

    async def test_health_makes_no_store_or_postgres_call(self, monkeypatch):
        vs = FakeVectorStore(exists=True, has_data=True, stamp=MATCHING_STAMP, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=MATCHING_STAMP)
        _install(vs, gs, monkeypatch)
        await ig.run_check()
        vs.observe_calls = 0
        gs.observe_calls = 0
        client = TestClient(app)
        client.get("/health")
        assert vs.observe_calls == 0
        assert gs.observe_calls == 0


class TestRefreshLoop:
    async def test_reindex_required_moves_to_ok_once_restamped_elsewhere(self, monkeypatch):
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required", reason="stale"))
        vs = FakeVectorStore(exists=True, has_data=True, stamp=MATCHING_STAMP, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=MATCHING_STAMP)
        _install(vs, gs, monkeypatch)
        status = await ig.refresh()
        assert status.state == "ok"

    async def test_ok_status_skips_stamp_re_observation(self, monkeypatch):
        vs = FakeVectorStore(exists=True, has_data=True, stamp=MATCHING_STAMP, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=MATCHING_STAMP)
        _install(vs, gs, monkeypatch)
        await ig.run_check()
        vs.observe_calls = 0
        gs.observe_calls = 0
        await ig.refresh()
        assert vs.observe_calls == 0
        assert gs.observe_calls == 0

    async def test_not_ok_status_reobserves_on_refresh(self, monkeypatch):
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("reindex_required", reason="stale"))
        vs = FakeVectorStore(exists=True, has_data=True, stamp=MATCHING_STAMP, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=MATCHING_STAMP)
        _install(vs, gs, monkeypatch)
        await ig.refresh()
        assert vs.observe_calls == 1
        assert gs.observe_calls == 1


class TestPreparingSkipsStampWrite:
    async def test_run_check_writes_no_stamp_when_lock_is_held_exclusively(self, monkeypatch):
        vs = FakeVectorStore(exists=False, has_data=False, stamp=None)
        gs = FakeGraphStore(exists=True, has_data=False, stamp=None)
        _install(vs, gs, monkeypatch)

        async def _probe():
            return "exclusive"

        monkeypatch.setattr(maintenance_lock, "probe", _probe)

        async def _acquire(mode):
            return None  # another process holds it

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)

        await ig.run_check()
        assert gs.written_stamps == []
        assert vs.written_stamps == []


class TestNoPostgresStampsNormally:
    async def test_fresh_stores_are_stamped_and_status_is_ok_not_stuck_rebuilding(self, monkeypatch):
        vs = FakeVectorStore(exists=False, has_data=False, stamp=None)
        gs = FakeGraphStore(exists=True, has_data=False, stamp=None)
        _install(vs, gs, monkeypatch)
        # maintenance_lock.get_pool is already patched to None by the
        # autouse fixture, so acquire() returns a no-op handle.
        status = await ig.run_check()
        assert status.state == "ok"
        assert gs.written_stamps == [MATCHING_STAMP]

    async def test_shared_lock_none_skips_the_write(self, monkeypatch):
        vs = FakeVectorStore(exists=False, has_data=False, stamp=None)
        gs = FakeGraphStore(exists=True, has_data=False, stamp=None)
        _install(vs, gs, monkeypatch)

        async def _acquire(mode):
            return None

        monkeypatch.setattr(maintenance_lock, "acquire", _acquire)
        await ig.run_check()
        assert gs.written_stamps == []


class TestUnverified:
    """Existing (pre-1.0.0) data, embedder down: `/search` works, index jobs
    are refused, and no stamp is written until verification actually runs
    (ADR-004 §3 FR-011)."""

    async def test_embedder_down_is_unverified_search_allowed_jobs_refused(self, monkeypatch):
        vs = FakeVectorStore(exists=True, has_data=True, stamp=None, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=None)
        _install(vs, gs, monkeypatch)

        async def _down(texts):
            raise ConnectionRefusedError("embedding service down")

        monkeypatch.setattr(retriever, "sample_chunks", lambda n, scan_limit=20: _one_short_sample())
        monkeypatch.setattr(embedder, "embed", _down)

        status = await ig.run_check()
        assert status.state == "unverified"
        assert vs.written_stamps == []
        assert gs.written_stamps == []
        assert ig.require_searchable() is None  # /search still works

        gate = await ig.require_writable()
        assert gate is not None
        assert gate.status_code == 409
        import json
        body = json.loads(gate.body)
        assert body["index_status"] == "unverified"
        assert "Index unverified" in body["detail"]

    async def test_embedder_recovers_inline_recheck_stamps_and_allows(self, monkeypatch):
        vs = FakeVectorStore(exists=True, has_data=True, stamp=None, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=None)
        _install(vs, gs, monkeypatch)

        down = {"value": True}

        async def _maybe_down(texts):
            if down["value"]:
                raise ConnectionRefusedError("embedding service down")
            return [[1.0, 0.0]] * len(texts)

        monkeypatch.setattr(retriever, "sample_chunks", lambda n, scan_limit=20: _one_short_sample(dim=2))
        monkeypatch.setattr(embedder, "embed", _maybe_down)

        await ig.run_check()
        assert ig.status().state == "unverified"

        down["value"] = False
        gate = await ig.require_writable()  # the inline re-check (FR-011)
        assert gate is None  # now allowed
        assert ig.status().state == "ok"
        assert gs.written_stamps == [ig.IndexStamp(1, MODEL, DIM)]

    async def test_refresh_loop_moves_unverified_to_ok_once_intervals_elapse(self, monkeypatch):
        vs = FakeVectorStore(exists=True, has_data=True, stamp=None, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=None)
        _install(vs, gs, monkeypatch)
        monkeypatch.setenv("INDEX_VERIFY_INTERVAL_SECONDS", "0.01")
        monkeypatch.setattr(retriever, "sample_chunks", lambda n, scan_limit=20: _one_short_sample(dim=2))

        async def _down(texts):
            raise ConnectionRefusedError("embedding service down")

        monkeypatch.setattr(embedder, "embed", _down)
        await ig.run_check()
        assert ig.status().state == "unverified"

        async def _ok(texts):
            return [[1.0, 0.0]] * len(texts)

        monkeypatch.setattr(embedder, "embed", _ok)
        await asyncio.sleep(0.02)  # let the retry interval elapse
        await ig.refresh()
        assert ig.status().state == "ok"

    async def test_refresh_loop_moves_unverified_to_reindex_required_when_verification_fails(self, monkeypatch):
        vs = FakeVectorStore(exists=True, has_data=True, stamp=None, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=None)
        _install(vs, gs, monkeypatch)
        monkeypatch.setenv("INDEX_VERIFY_INTERVAL_SECONDS", "0.01")
        monkeypatch.setattr(retriever, "sample_chunks", lambda n, scan_limit=20: _one_short_sample(dim=2))

        async def _down(texts):
            raise ConnectionRefusedError("embedding service down")

        monkeypatch.setattr(embedder, "embed", _down)
        await ig.run_check()
        assert ig.status().state == "unverified"

        async def _wrong(texts):
            return [[0.0, 1.0]] * len(texts)  # orthogonal to the stored vector

        monkeypatch.setattr(embedder, "embed", _wrong)
        await asyncio.sleep(0.02)  # let the retry interval elapse
        await ig.refresh()
        assert ig.status().state == "reindex_required"

    async def test_refresh_is_idle_when_status_is_ok(self, monkeypatch):
        vs = FakeVectorStore(exists=True, has_data=True, stamp=MATCHING_STAMP, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=MATCHING_STAMP)
        _install(vs, gs, monkeypatch)
        await ig.run_check()
        assert ig.status().state == "ok"

        call_count = {"n": 0}

        async def _tracked(n, scan_limit=20):
            call_count["n"] += 1
            return []

        monkeypatch.setattr(retriever, "sample_chunks", _tracked)
        await ig.refresh()
        assert call_count["n"] == 0

    async def test_concurrent_require_writable_serializes_through_the_lock(self, monkeypatch):
        vs = FakeVectorStore(exists=True, has_data=True, stamp=None, schema_dim=DIM)
        gs = FakeGraphStore(exists=True, has_data=True, stamp=None)
        _install(vs, gs, monkeypatch)
        monkeypatch.setattr(ig, "_status", ig.IndexStatus("unverified", reason="not yet verified"))

        active = {"n": 0}
        max_concurrent = {"n": 0}

        async def _slow_sample(n, scan_limit=20):
            active["n"] += 1
            max_concurrent["n"] = max(max_concurrent["n"], active["n"])
            await asyncio.sleep(0.01)
            active["n"] -= 1
            return [("short", [1.0, 0.0, 0.0, 0.0])]

        async def _ok(texts):
            return [[1.0, 0.0, 0.0, 0.0]]

        monkeypatch.setattr(retriever, "sample_chunks", _slow_sample)
        monkeypatch.setattr(embedder, "embed", _ok)

        await asyncio.gather(ig.require_writable(), ig.require_writable())
        assert max_concurrent["n"] == 1  # never two checks running at once
