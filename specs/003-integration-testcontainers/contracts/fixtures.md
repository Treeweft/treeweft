# Contract: integration service fixtures and switches

## Environment

| Variable | Meaning |
|---|---|
| `TREEWEFT_ITEST_CONTAINERS=1` | Self-provisioned mode: start throwaway services for the services this run needs |
| `POSTGRES_TEST_URL` | Explicit Postgres (`postgresql://…`); wins over provisioning for Postgres |
| `MILVUS_TEST_URI` | Explicit Milvus (`http://host:19530`); wins for Milvus |
| `NEO4J_TEST_URI` (+ `NEO4J_USER`, `NEO4J_PASSWORD`) | Explicit Neo4j; wins for Neo4j |
| `TESTCONTAINERS_*` | The library's own overrides (Ryuk, socket, host); documented, not set by the suite |

Resolution per service: explicit variable → switch → skip. The skip reason names both options,
for example: "Postgres not configured: set POSTGRES_TEST_URL, or TREEWEFT_ITEST_CONTAINERS=1 to
start a throwaway one (needs Docker)".

## Fixtures (`tests/integration/conftest.py`, all session-scoped, synchronous)

```python
@pytest.fixture(scope="session")
def postgres_url() -> str                 # plain postgresql://user:pw@127.0.0.1:<port>/db (asyncpg-ready)

@pytest.fixture(scope="session")
def milvus_uri() -> str                   # http://127.0.0.1:<port>

@dataclass(frozen=True)
class Neo4jConn:
    uri: str                              # bolt://127.0.0.1:<port>
    user: str
    password: str

@pytest.fixture(scope="session")
def neo4j_conn() -> Neo4jConn

@pytest.fixture
def neo4j_graph_store(neo4j_conn, monkeypatch)   # the graph_store module pointed at neo4j_conn:
                                                 # NEO4J_URI/USER/PASSWORD module attributes patched,
                                                 # _driver reset before and after
```

Guarantees in self-provisioned mode:

- the image comes from `docker-compose.yml` (`services.<name>.image`);
- every published port is bound to `127.0.0.1` on a random host port;
- credentials are generated per run, never read from the environment or `.env`;
- the service is healthy before the fixture returns, or the run fails with the service's logs;
- it is stopped at session end, with its logs written to `.itest-logs/<service>.log` if any test
  failed. Ryuk removes it within about 10 s if the process dies.

## Helpers (unit-testable, no Docker)

```python
def compose_image(service: str, compose_path: Path = REPO / "docker-compose.yml") -> str
def bind_loopback(ports: dict) -> dict        # {5432: None} -> {5432: ("127.0.0.1", None)}
def resolve_mode(service: str, env: Mapping[str, str]) -> Literal["explicit", "container", "skip"]
```

These live in `tests/integration/_services.py`. The unit tests for them live in
`tests/unit/test_integration_services.py`: it imports the helpers only, never testcontainers'
container classes, and never needs Docker.

## CI job (`.github/workflows/ci.yml`)

```yaml
integration:
  name: integration            # static: required-check-ready (ruleset adds it later; advisory now)
  needs: changes
  if: needs.changes.outputs.code == 'true'
  runs-on: ubuntu-latest
  steps: checkout → setup-uv (pinned) → uv sync --locked --extra dev --extra all-backends --extra simple
         → cp .env.example .env
         → TREEWEFT_ITEST_CONTAINERS=1 uv run --locked --no-sync pytest tests/integration -m slow -q
         → on failure: upload-artifact .itest-logs/
```
