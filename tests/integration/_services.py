"""Pure helpers for integration test service resolution (no Docker, no testcontainers)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Mapping

import yaml

REPO = Path(__file__).resolve().parents[2]

SERVICE_VARS = {
    "postgres": "POSTGRES_TEST_URL",
    "milvus": "MILVUS_TEST_URI",
    "neo4j": "NEO4J_TEST_URI",
}

SWITCH = "TREEWEFT_ITEST_CONTAINERS"

_HUMAN_NAMES = {
    "postgres": "Postgres",
    "milvus": "Milvus",
    "neo4j": "Neo4j",
}


def compose_image(service: str, compose_path: Path = REPO / "docker-compose.yml") -> str:
    with open(compose_path, "r") as f:
        compose = yaml.safe_load(f)
    services = (compose or {}).get("services", {})
    if service not in services:
        raise ValueError(f"Service '{service}' not found in compose file {compose_path}")
    image = services[service].get("image")
    if not image:
        raise ValueError(f"Service '{service}' has no image in compose file {compose_path}")
    return image


def bind_loopback(ports: dict) -> dict:
    return {port: ("127.0.0.1", None) for port in ports}


def resolve_mode(service: str, env: Mapping[str, str]) -> Literal["explicit", "container", "skip"]:
    var = SERVICE_VARS[service]
    if env.get(var):
        return "explicit"
    if env.get(SWITCH) == "1":
        return "container"
    return "skip"


def skip_reason(service: str) -> str:
    var = SERVICE_VARS[service]
    name = _HUMAN_NAMES[service]
    return (
        f"{name} not configured: set {var}, or {SWITCH}=1 to start a throwaway one "
        "(needs Docker)"
    )
