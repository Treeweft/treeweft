"""The docker-compose indexer gets its settings from .env, with the service
addresses replaced for the container (#48).

It used to receive only the variables listed in `environment:`, so it
ignored most of .env and could not start: MILVUS_HOST, MILVUS_PORT and
EMBEDDING_URL were never passed. Parses the YAML; no Docker needed.
"""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Settings in .env.example with a loopback address that the indexer never
# reads, so the container does not need them replaced.
NOT_READ_BY_THE_INDEXER = {
    "INDEXER_URL",            # the MCP server and host CLI tools
    "TREEWEFT_API_BASE",      # host CLI tools
    "TREEWEFT_CORS_ORIGINS",  # a browser origin, not an address the indexer calls
}


def _indexer() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["indexer"]


def _environment() -> dict[str, str]:
    return dict(str(e).split("=", 1) for e in _indexer()["environment"])


def _env_example() -> dict[str, str]:
    pairs = re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", (ROOT / ".env.example").read_text(), re.M)
    return dict(pairs)


def test_indexer_reads_the_env_file():
    env_file = _indexer()["env_file"]
    assert {"path": ".env", "required": False} in env_file


def test_every_loopback_address_in_env_example_is_replaced_for_the_container():
    """A host address in .env is the container itself inside it. Each one
    must be replaced in `environment:`, which wins over env_file."""
    loopback = {
        name for name, value in _env_example().items()
        if re.search(r"\b(localhost|127\.0\.0\.1)\b", value) and not name.startswith("DOCKER_")
    }
    assert loopback, "found no loopback addresses in .env.example; the parser is broken"

    not_replaced = loopback - set(_environment()) - NOT_READ_BY_THE_INDEXER
    assert not not_replaced, (
        f"{sorted(not_replaced)} hold a loopback address in .env.example and would reach "
        "the indexer container unchanged; replace them in docker-compose.yml "
        "(services.indexer.environment) or, if the indexer does not read them, add them "
        "to NOT_READ_BY_THE_INDEXER"
    )


def test_replaced_addresses_do_not_point_at_loopback():
    for name, value in _environment().items():
        assert not re.search(r"\b(localhost|127\.0\.0\.1)\b", value), f"{name}={value}"


def test_the_settings_startup_requires_are_set():
    """validate_config() requires these with the default backends; the
    compose file used to pass none of the first three."""
    for name in ("MILVUS_HOST", "MILVUS_PORT", "EMBEDDING_URL", "NEO4J_URI", "LLM_URL", "RERANKER_URL"):
        assert name in _environment(), name


def test_llm_on_the_host_is_reached_through_host_docker_internal():
    assert "host.docker.internal:host-gateway" in _indexer()["extra_hosts"]
    assert "host.docker.internal" in _env_example()["DOCKER_LLM_URL"]
