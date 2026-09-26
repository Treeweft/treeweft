"""Session fixtures provisioning throwaway Postgres, Milvus and Neo4j services
for tests/integration (contracts/fixtures.md, research R5-R9, R12).

Resolution per service is explicit variable -> container -> skip
(`_services.resolve_mode`). Container mode never reads `.env` for an
address or credential: images come from `docker-compose.yml`
(`_services.compose_image`), ports are bound to 127.0.0.1 only
(`_services.bind_loopback`), and credentials are generated per run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import docker
import pytest
from testcontainers.community.milvus import MilvusContainer
from testcontainers.community.neo4j import Neo4jContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.config import testcontainers_config as tc_config

from tests.integration._services import bind_loopback, compose_image, resolve_mode, skip_reason

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = REPO_ROOT / ".itest-logs"

logger = logging.getLogger(__name__)


@pytest.fixture(scope="session")
def docker_available() -> None:
    """Fail loudly (not skip) if Docker isn't reachable (FR-006).

    Only pulled in by the service fixtures below, and only once they've
    resolved to container mode -- explicit and skip modes never touch Docker.
    """
    try:
        docker.from_env().ping()
    except docker.errors.DockerException as exc:
        pytest.fail(f"Docker is required when TREEWEFT_ITEST_CONTAINERS=1: {exc}")


@pytest.fixture(scope="session")
def _itest_containers() -> dict:
    """service name -> started container object, for test_provisioning.py to inspect."""
    return {}


def _read_logs(container) -> str:
    stdout, stderr = container.get_logs()
    return (stdout + b"\n" + stderr).decode(errors="replace")


def _tail_logs(container, lines: int = 200) -> str:
    return "\n".join(_read_logs(container).splitlines()[-lines:])


def _write_failure_log(service: str, container) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / f"{service}.log").write_text(_read_logs(container))


@contextmanager
def _startup_timeout_seconds(seconds: float) -> Iterator[None]:
    """Override testcontainers' global startup timeout for one `start()` call.

    Postgres and Neo4j build their wait strategy inside their own `_connect()`
    and read the library's global `testcontainers_config.timeout` (a computed,
    read-only property), so there is no per-instance hook to pass a timeout
    through -- this overrides `max_tries` for the duration of the call instead.
    """
    original = tc_config.max_tries
    tc_config.max_tries = max(1, round(seconds / tc_config.sleep_time))
    try:
        yield
    finally:
        tc_config.max_tries = original


def _start_container(name: str, container, image: str, timeout: float) -> None:
    start = time.monotonic()
    try:
        with _startup_timeout_seconds(timeout):
            container.start()
    except TimeoutError:
        pytest.fail(
            f"{name} container ({image}) did not become ready within {timeout:.0f}s. "
            f"Last 200 log lines:\n{_tail_logs(container)}"
        )
    except docker.errors.DockerException as exc:
        pytest.fail(f"Failed to start {name} container (image {image}): {exc}")
    logger.info("%s container ready in %.1fs (image %s)", name, time.monotonic() - start, image)


def _register_teardown(request: pytest.FixtureRequest, service: str, container) -> None:
    def _teardown() -> None:
        if request.session.testsfailed:
            _write_failure_log(service, container)
        container.stop()

    request.addfinalizer(_teardown)


@pytest.fixture(scope="session")
def postgres_url(request: pytest.FixtureRequest, _itest_containers: dict) -> str:
    mode = resolve_mode("postgres", os.environ)
    if mode == "explicit":
        return os.environ["POSTGRES_TEST_URL"]
    if mode == "skip":
        pytest.skip(skip_reason("postgres"))

    request.getfixturevalue("docker_available")

    image = compose_image("postgres")
    password = secrets.token_hex(16)
    container = PostgresContainer(image, username="itest", password=password, dbname="itest", driver=None)
    container.ports = bind_loopback(container.ports)

    _start_container("postgres", container, image, 60)
    _itest_containers["postgres"] = container
    _register_teardown(request, "postgres", container)

    port = container.get_exposed_port(container.port)
    return f"postgresql://itest:{password}@127.0.0.1:{port}/itest"


@pytest.fixture(scope="session")
def milvus_uri(request: pytest.FixtureRequest, _itest_containers: dict) -> str:
    mode = resolve_mode("milvus", os.environ)
    if mode == "explicit":
        return os.environ["MILVUS_TEST_URI"]
    if mode == "skip":
        pytest.skip(skip_reason("milvus"))

    request.getfixturevalue("docker_available")

    image = compose_image("milvus")
    container = MilvusContainer(image)
    container.ports = bind_loopback(container.ports)

    _start_container("milvus", container, image, 180)
    _itest_containers["milvus"] = container
    _register_teardown(request, "milvus", container)

    port = container.get_exposed_port(container.port)
    return f"http://127.0.0.1:{port}"


@dataclass(frozen=True)
class Neo4jConn:
    uri: str
    user: str
    password: str


@pytest.fixture(scope="session")
def neo4j_conn(request: pytest.FixtureRequest, _itest_containers: dict) -> Neo4jConn:
    mode = resolve_mode("neo4j", os.environ)
    if mode == "explicit":
        return Neo4jConn(
            uri=os.environ["NEO4J_TEST_URI"],
            user=os.environ.get("NEO4J_USER", "neo4j"),
            password=os.environ.get("NEO4J_PASSWORD", ""),
        )
    if mode == "skip":
        pytest.skip(skip_reason("neo4j"))

    request.getfixturevalue("docker_available")

    image = compose_image("neo4j")
    password = secrets.token_hex(16)
    container = Neo4jContainer(image, password=password)
    container.ports = bind_loopback(container.ports)

    _start_container("neo4j", container, image, 120)
    _itest_containers["neo4j"] = container
    _register_teardown(request, "neo4j", container)

    port = container.get_exposed_port(container.port)
    return Neo4jConn(uri=f"bolt://127.0.0.1:{port}", user="neo4j", password=password)


@pytest.fixture
def neo4j_graph_store(neo4j_conn: Neo4jConn, monkeypatch: pytest.MonkeyPatch):
    """The graph_store module redirected at `neo4j_conn`, never at `.env`."""
    from treeweft.adapters.neo4j import graph_store

    monkeypatch.setattr(graph_store, "NEO4J_URI", neo4j_conn.uri)
    monkeypatch.setattr(graph_store, "NEO4J_USER", neo4j_conn.user)
    monkeypatch.setattr(graph_store, "NEO4J_PASSWORD", neo4j_conn.password)
    graph_store._driver = None
    yield graph_store
    if graph_store._driver is not None:
        asyncio.run(graph_store.close())
    graph_store._driver = None
