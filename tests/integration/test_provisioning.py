"""Self-test for tests/integration/conftest.py's service fixtures
(contracts/fixtures.md "Guarantees", research R10).

Only this file inspects the containers directly, through the conftest's
`_itest_containers` registry -- never module globals. It skips per service
in explicit mode, where there is nothing self-provisioned to inspect.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

import pytest

from tests.integration._services import compose_image, resolve_mode

pytestmark = [pytest.mark.slow]


def _published_host_ips(container) -> list[str]:
    wrapped = container.get_wrapped_container()
    wrapped.reload()
    port_bindings = wrapped.attrs["NetworkSettings"]["Ports"]
    ips = []
    for bindings in port_bindings.values():
        for binding in bindings or []:
            ips.append(binding["HostIp"])
    return ips


def test_postgres_provisioning(request: pytest.FixtureRequest) -> None:
    if resolve_mode("postgres", os.environ) != "container":
        pytest.skip("postgres not resolved to container mode")

    url = request.getfixturevalue("postgres_url")
    container = request.getfixturevalue("_itest_containers")["postgres"]

    ips = _published_host_ips(container)
    assert ips, "expected at least one published port"
    assert set(ips) == {"127.0.0.1"}
    assert container.image == compose_image("postgres")

    parsed = urlparse(url)
    assert parsed.username == "itest"
    assert parsed.password != os.environ.get("POSTGRES_PASSWORD")

    import asyncpg

    async def _select_one() -> int:
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetchval("select 1")
        finally:
            await conn.close()

    assert asyncio.run(_select_one()) == 1


def test_milvus_provisioning(request: pytest.FixtureRequest) -> None:
    if resolve_mode("milvus", os.environ) != "container":
        pytest.skip("milvus not resolved to container mode")

    uri = request.getfixturevalue("milvus_uri")
    container = request.getfixturevalue("_itest_containers")["milvus"]

    ips = _published_host_ips(container)
    assert ips, "expected at least one published port"
    assert set(ips) == {"127.0.0.1"}
    assert container.image == compose_image("milvus")

    from pymilvus import MilvusClient

    client = MilvusClient(uri=uri)
    client.list_collections()


def test_neo4j_provisioning(request: pytest.FixtureRequest) -> None:
    if resolve_mode("neo4j", os.environ) != "container":
        pytest.skip("neo4j not resolved to container mode")

    conn = request.getfixturevalue("neo4j_conn")
    container = request.getfixturevalue("_itest_containers")["neo4j"]

    ips = _published_host_ips(container)
    assert ips, "expected at least one published port"
    assert set(ips) == {"127.0.0.1"}
    assert container.image == compose_image("neo4j")
    assert conn.password != os.environ.get("NEO4J_PASSWORD")

    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(conn.uri, auth=(conn.user, conn.password))
    try:
        driver.verify_connectivity()
    finally:
        driver.close()
