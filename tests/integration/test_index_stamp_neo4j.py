"""Index stamp round-trip against a real Neo4j (ADR-004 §3, research R12).

`_META_ID` is patched to a per-run value, and every clear this test runs
is scoped to its own run prefix via `_clear_index_data(scope_prefix=...)`
— it never runs an unscoped clear against a shared database (constitution
II), and never touches a real deployment's own stamp (which stays at the
real `_META_ID = "index"`).

Run against a standalone instance:

    docker compose --profile local-infra up -d neo4j
    NEO4J_TEST_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=... \\
      env -u PYTHONPATH python -m pytest tests/integration/test_index_stamp_neo4j.py -v

Skipped unless NEO4J_TEST_URI is set — no service, no silent pass.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

URI = os.environ.get("NEO4J_TEST_URI")
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not URI, reason="set NEO4J_TEST_URI to run"),
    pytest.mark.asyncio,
]

RUN = f"itest-stamp-{uuid.uuid4().hex[:8]}"
MODEL = "Qwen/Qwen3-Embedding-0.6B"


@pytest_asyncio.fixture
async def gs():
    os.environ["NEO4J_URI"] = URI
    from treeweft.adapters.neo4j import graph_store

    graph_store._driver = None  # bind to this test's event loop
    original_meta_id = graph_store._META_ID
    graph_store._META_ID = RUN  # never touch a real deployment's stamp
    await graph_store.ensure_schema()
    yield graph_store
    # Scoped teardown only — never an unscoped clear (constitution II).
    await graph_store._clear_index_data(scope_prefix=RUN)
    async with graph_store._get_session() as session:
        await session.run("MATCH (m:TreeweftMeta {id: $id}) DETACH DELETE m", id=RUN)
    graph_store._META_ID = original_meta_id
    await graph_store.close()


def _entity(name: str) -> dict:
    return {
        "id": f"{RUN}:{name}",
        "type": "Function",
        "name": name,
        "file_path": f"/{RUN}/{name}.py",
        "start_line": 1,
        "end_line": 5,
        "signature": f"def {name}()",
        "language": "python",
    }


class TestStampRoundTrip:
    async def test_write_then_read(self, gs):
        from treeweft.domain.index_stamp import IndexStamp

        stamp = IndexStamp(schema=1, embedding_model=MODEL, vector_dim=1024)
        await gs.write_stamp(stamp)
        assert await gs.read_stamp() == stamp

    async def test_observe_index_reports_the_stamp(self, gs):
        from treeweft.domain.index_stamp import IndexStamp

        stamp = IndexStamp(schema=1, embedding_model=MODEL, vector_dim=1024)
        await gs.write_stamp(stamp)
        obs = await gs.observe_index()
        assert obs.stamp == stamp


class TestConstraintExists:
    async def test_treeweft_meta_constraint_is_created(self, gs):
        async with gs._get_session() as session:
            result = await session.run("SHOW CONSTRAINTS YIELD name RETURN name")
            names = {r["name"] async for r in result}
        assert "treeweft_meta_id_unique" in names


class TestScopedClearSparesOthersAndTheMetaNode:
    async def test_clear_index_data_scoped_to_this_run_only(self, gs):
        await gs.store_graph([_entity("a")], [], source_id=f"{RUN}:src")
        from treeweft.domain.index_stamp import IndexStamp

        await gs.write_stamp(IndexStamp(schema=1, embedding_model=MODEL, vector_dim=1024))

        other_run = f"itest-stamp-sibling-{uuid.uuid4().hex[:8]}"
        async with gs._get_session() as session:
            await session.run(
                "CREATE (:Entity {id: $id, type: 'Function', name: 'sibling'})",
                id=f"{other_run}:sibling",
            )
        try:
            await gs._clear_index_data(scope_prefix=RUN)

            async with gs._get_session() as session:
                result = await session.run(
                    "MATCH (e:Entity {id: $id}) RETURN count(e) AS n", id=f"{RUN}:a"
                )
                assert (await result.single())["n"] == 0

                result = await session.run(
                    "MATCH (e:Entity {id: $id}) RETURN count(e) AS n", id=f"{other_run}:sibling"
                )
                assert (await result.single())["n"] == 1  # sibling survives — never unscoped

                result = await session.run(
                    "MATCH (m:TreeweftMeta {id: $id}) RETURN count(m) AS n", id=RUN
                )
                assert (await result.single())["n"] == 1  # the meta node itself is spared
        finally:
            async with gs._get_session() as session:
                await session.run(
                    "MATCH (n) WHERE n.id STARTS WITH $run DETACH DELETE n", run=other_run
                )
