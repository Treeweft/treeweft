"""Neo4j deletes reap the ExternalModule import targets they leave dangling.

Integration test: runs the real Cypher against a live Neo4j, because the
unit tests (tests/unit/test_neo4j_orphan_cleanup.py) can only check query
text. Run against throwaway or explicit services via
`tests/integration/conftest.py`'s `neo4j_graph_store` fixture —
self-provisioned (`TREEWEFT_ITEST_CONTAINERS=1`) or explicit
(`NEO4J_TEST_URI` + `NEO4J_USER`/`NEO4J_PASSWORD`). Skipped with neither set.

Every node it creates carries a per-run id prefix and is removed afterwards;
it never touches other data in the database.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

pytestmark = [
    pytest.mark.slow,
]

RUN = f"itest-orphan-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def gs(neo4j_graph_store):
    graph_store = neo4j_graph_store
    yield graph_store
    async with graph_store._get_session() as session:
        await session.run(
            "MATCH (n) WHERE n.id STARTS WITH $run DETACH DELETE n", run=RUN
        )
    await graph_store.close()


def _entity(name: str, file_path: str) -> dict:
    return {
        "id": f"{RUN}:{name}",
        "type": "Function",
        "name": name,
        "file_path": f"/{RUN}{file_path}",
        "start_line": 1,
        "end_line": 5,
        "signature": f"def {name}()",
        "language": "python",
    }


async def _store(gs, entity: dict, targets: list[str], source: str, rel: str = "IMPORTS"):
    await gs.store_graph(
        [entity],
        [{"source_id": entity["id"], "target_id": t, "type": rel} for t in targets],
        source_id=f"{RUN}:{source}",
    )


async def _exists(gs, node_id: str) -> bool:
    async with gs._get_session() as session:
        result = await session.run("MATCH (n {id: $id}) RETURN count(n) AS n", id=node_id)
        rec = await result.single()
        return rec["n"] > 0


@pytest.mark.asyncio
async def test_delete_source_reaps_only_dangling_external_modules(gs):
    only_a, shared, legacy = f"{RUN}:ext-only-a", f"{RUN}:ext-shared", f"{RUN}:ext-legacy"
    await _store(gs, _entity("a", "/a.py"), [only_a, shared], "src-a")
    await _store(gs, _entity("b", "/b.py"), [shared], "src-b")
    async with gs._get_session() as session:  # a pre-existing, unrelated orphan
        await session.run(
            "CREATE (:Entity {id: $id, type: 'ExternalModule'})", id=legacy
        )

    await gs.delete_source(f"{RUN}:src-a")

    assert not await _exists(gs, f"{RUN}:a")
    assert not await _exists(gs, only_a)       # dangling -> reaped
    assert await _exists(gs, shared)           # src-b still imports it
    assert await _exists(gs, legacy)           # not a neighbour: no global sweep


@pytest.mark.asyncio
async def test_delete_entities_by_file_reaps_dangling_external_modules(gs):
    only_f2, both = f"{RUN}:ext-only-f2", f"{RUN}:ext-both"
    await _store(gs, _entity("f1", "/f1.py"), [both], "src")
    await _store(gs, _entity("f2", "/f2.py"), [both, only_f2], "src")

    deleted = await gs.delete_entities_by_file(f"{RUN}:src", f"/{RUN}/f2.py")

    assert deleted == 2  # f2's entity + the import target it orphaned
    assert not await _exists(gs, only_f2)
    assert await _exists(gs, both)


@pytest.mark.asyncio
async def test_delete_file_entities_reaps_dangling_external_modules(gs):
    ext = f"{RUN}:ext-f"
    await _store(gs, _entity("f", "/f.py"), [ext], "src")
    await gs.delete_file_entities(f"/{RUN}/f.py")
    assert not await _exists(gs, ext)


@pytest.mark.asyncio
async def test_defined_call_target_is_not_reaped(gs):
    callee = _entity("callee", "/callee.py")
    await _store(gs, callee, [], "src")
    await _store(gs, _entity("caller", "/caller.py"), [callee["id"]], "src", rel="CALLS")
    await gs.delete_entities_by_file(f"{RUN}:src", f"/{RUN}/caller.py")
    assert await _exists(gs, callee["id"])
