"""docker-compose.yml: how the mcp-server and ui containers reach the indexer.

Parses the YAML; no Docker needed.
"""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _service(name: str) -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"][name]


def _environment(name: str) -> dict[str, str]:
    return dict(str(e).split("=", 1) for e in _service(name).get("environment") or [])


def _env_example() -> dict[str, str]:
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", (ROOT / ".env.example").read_text(), re.M))


def test_mcp_server_reaches_the_compose_indexer_by_default():
    assert _environment("mcp-server")["INDEXER_URL"].startswith("${DOCKER_INDEXER_URL")
    assert _env_example()["DOCKER_INDEXER_URL"] == "http://indexer:8001"


def test_mcp_server_never_receives_the_transport_or_a_caller_token():
    """TREEWEFT_MCP_TRANSPORT would clash with the transport this container
    runs (CLAUDE.md), and TREEWEFT_MCP_TOKEN is one caller's own credential:
    a shared HTTP server using it would lend that caller's access to every
    client. An env_file would pass both, so the service has none."""
    service = _service("mcp-server")
    assert "env_file" not in service
    env = _environment("mcp-server")
    assert "TREEWEFT_MCP_TRANSPORT" not in env
    assert "TREEWEFT_MCP_TOKEN" not in env


def test_mcp_server_does_not_start_an_indexer_of_its_own():
    """With an indexer on the host, depends_on would start a second one."""
    assert "depends_on" not in _service("mcp-server")


def test_ui_api_base_is_a_browser_address():
    """The browser, not the container, calls the indexer."""
    default = _environment("ui")["TREEWEFT_API_BASE"]
    assert "localhost:8001" in default
    assert "indexer:" not in default
