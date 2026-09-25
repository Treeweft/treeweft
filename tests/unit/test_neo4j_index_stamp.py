"""Neo4j graph store index stamp (ADR-004 §3): the TreeweftMeta node.

Mocked at the session boundary, like test_graph_store_adapter.py's
test_store_graph_calls_neo4j — no real driver.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from treeweft.adapters.neo4j import graph_store as gs
from treeweft.domain.index_stamp import IndexStamp, UnreadableStamp


STAMP = IndexStamp(schema=1, embedding_model="Qwen/Qwen3-Embedding-0.6B", vector_dim=1024)


def _session(fetch_results):
    """A fake session whose `run()` returns results from `fetch_results` in
    order, one per call. Each entry is either a list of record-dicts or an
    exception instance to raise."""
    mock_session = AsyncMock()
    calls = []

    async def _run(query, **params):
        calls.append((query, params))
        outcome = fetch_results[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        mock_result = AsyncMock()

        async def _fetch(n):
            assert isinstance(n, int)
            records = []
            for rec in outcome:
                m = MagicMock()
                m.__getitem__ = MagicMock(side_effect=lambda k, rec=rec: rec[k])
                m.keys = MagicMock(return_value=list(rec.keys()))
                records.append(m)
            return records[:n]

        mock_result.fetch = _fetch
        mock_result.consume = AsyncMock()
        return mock_result

    mock_session.run = AsyncMock(side_effect=_run)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    return mock_session, calls


def _patched(mock_session):
    return patch("treeweft.adapters.neo4j.graph_store._get_session", return_value=mock_session)


class TestSchema:
    def test_schema_statements_include_meta_constraint(self):
        joined = " ".join(gs._SCHEMA_STATEMENTS)
        assert "CREATE CONSTRAINT treeweft_meta_id_unique IF NOT EXISTS" in joined
        assert "FOR (m:TreeweftMeta) REQUIRE m.id IS UNIQUE" in joined


class TestReadStamp:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_row(self):
        session, calls = _session([[]])
        with _patched(session):
            assert await gs.read_stamp() is None

    @pytest.mark.asyncio
    async def test_returns_parsed_stamp(self):
        row = {"index_schema": 1, "embedding_model": "Qwen/Qwen3-Embedding-0.6B", "vector_dim": 1024}
        session, calls = _session([[row]])
        with _patched(session):
            assert await gs.read_stamp() == STAMP

    @pytest.mark.asyncio
    async def test_uses_the_meta_id_constant(self):
        session, calls = _session([[]])
        with _patched(session):
            await gs.read_stamp()
        assert calls[0][1].get("id") == gs._META_ID


class TestWriteStamp:
    @pytest.mark.asyncio
    async def test_issues_merge_with_meta_id(self):
        session, calls = _session([[]])
        with _patched(session):
            await gs.write_stamp(STAMP)
        query, params = calls[0]
        assert "MERGE (m:TreeweftMeta {id: $id})" in query
        assert "SET" in query
        assert params["id"] == gs._META_ID
        assert params["schema"] == 1
        assert params["embedding_model"] == "Qwen/Qwen3-Embedding-0.6B"
        assert params["vector_dim"] == 1024


class TestObserveIndex:
    @pytest.mark.asyncio
    async def test_has_data_false_when_no_entity(self):
        session, calls = _session([[], []])
        with _patched(session):
            obs = await gs.observe_index()
        assert obs.has_data is False
        assert obs.store == "graph"
        assert obs.backend == "neo4j"
        assert obs.exists is True

    @pytest.mark.asyncio
    async def test_entity_query_uses_fetch_one_with_limit(self):
        session, calls = _session([[{"1": 1}], []])
        with _patched(session):
            obs = await gs.observe_index()
        assert obs.has_data is True
        assert "MATCH (e:Entity) RETURN 1 LIMIT 1" in calls[0][0]

    @pytest.mark.asyncio
    async def test_returns_the_stamp(self):
        row = {"index_schema": 1, "embedding_model": "Qwen/Qwen3-Embedding-0.6B", "vector_dim": 1024}
        session, calls = _session([[], [row]])
        with _patched(session):
            obs = await gs.observe_index()
        assert obs.stamp == STAMP

    @pytest.mark.asyncio
    async def test_unreadable_stamp_is_reported(self):
        row = {"index_schema": "not-an-int", "embedding_model": "x", "vector_dim": 1024}
        session, calls = _session([[], [row]])
        with _patched(session):
            obs = await gs.observe_index()
        assert isinstance(obs.stamp, UnreadableStamp)

    @pytest.mark.asyncio
    async def test_driver_error_sets_unreachable_and_does_not_raise(self):
        session, calls = _session([ServiceUnavailableStub("connection refused")])
        with _patched(session):
            obs = await gs.observe_index()
        assert obs.unreachable is not None
        assert "connection refused" in obs.unreachable


class ServiceUnavailableStub(Exception):
    pass


class TestClearIndexData:
    def test_public_and_scoped_clear_spare_meta(self):
        joined = " ".join([gs._CLEAR_INDEX_DATA_QUERY_TEMPLATE.format(prefix_clause="")])
        assert "WHERE NOT n:TreeweftMeta" in joined

    @pytest.mark.asyncio
    async def test_clear_index_data_has_no_prefix_clause(self):
        session, calls = _session([[]])
        with _patched(session):
            await gs.clear_index_data()
        query, params = calls[0]
        assert "STARTS WITH" not in query
        assert "IN TRANSACTIONS OF 10000 ROWS" in query
        assert "WHERE NOT n:TreeweftMeta" in query

    @pytest.mark.asyncio
    async def test_scoped_clear_adds_prefix_clause_as_parameter(self):
        session, calls = _session([[]])
        with _patched(session):
            await gs._clear_index_data(scope_prefix="itest-x")
        query, params = calls[0]
        assert "AND n.id STARTS WITH $prefix" in query
        assert params["prefix"] == "itest-x"

    @pytest.mark.asyncio
    async def test_clear_all_spares_meta(self):
        session, calls = _session([[]])
        with _patched(session):
            await gs.clear_all()
        query, _ = calls[0]
        assert "WHERE NOT n:TreeweftMeta" in query
