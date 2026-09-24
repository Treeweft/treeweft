"""Neo4j deletes must also remove ExternalModule import targets they orphan.

Import targets are MERGEd by the relationship pass with type ExternalModule
and are never CONTAINed by a Source, so the per-source/per-file deletes left
them behind with no edges. Each delete now collects the ExternalModule
neighbours of what it deletes and, in the same statement, deletes those left
with no relationships. Behaviour against a real Neo4j is covered by
tests/integration/test_neo4j_orphan_cleanup.py.

Detroit-style: mock only neo4j (external).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest


def _session(mock_neo4j_driver):
    session = mock_neo4j_driver.session.return_value.__aenter__.return_value
    result = MagicMock()
    result.consume = AsyncMock(return_value=MagicMock(counters=MagicMock(nodes_deleted=0)))
    session.run.return_value = result
    return session


def _cyphers(session) -> list[str]:
    return [c.args[0] for c in session.run.call_args_list]


def _assert_scoped_orphan_cleanup(cypher: str):
    assert "ExternalModule" in cypher
    assert "NOT (x)--()" in cypher  # only nodes left with no relationships
    assert "MATCH (x:Entity" not in cypher  # scoped to neighbours, no global sweep


@pytest.mark.asyncio
async def test_delete_source_cleans_orphaned_external_modules(mock_neo4j_driver):
    from treeweft.adapters.neo4j.graph_store import delete_source

    session = _session(mock_neo4j_driver)
    await delete_source("src-1")
    entity_delete = next(c for c in _cyphers(session) if "CONTAINS" in c and "DETACH DELETE" in c)
    _assert_scoped_orphan_cleanup(entity_delete)


@pytest.mark.asyncio
async def test_delete_entities_by_file_cleans_orphaned_external_modules(mock_neo4j_driver):
    from treeweft.adapters.neo4j.graph_store import delete_entities_by_file

    session = _session(mock_neo4j_driver)
    await delete_entities_by_file("src-1", "a.py")
    _assert_scoped_orphan_cleanup(_cyphers(session)[-1])


@pytest.mark.asyncio
async def test_delete_file_entities_cleans_orphaned_external_modules(mock_neo4j_driver):
    from treeweft.adapters.neo4j.graph_store import delete_file_entities

    session = _session(mock_neo4j_driver)
    await delete_file_entities("a.py")
    _assert_scoped_orphan_cleanup(_cyphers(session)[-1])
