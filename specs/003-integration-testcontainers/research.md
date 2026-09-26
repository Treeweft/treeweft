# Research: Self-Provisioned Integration Test Services

Phase 0 of [plan.md](plan.md). Each item records a decision, the reasons for it, and the
alternatives considered. File references are to `origin/main` at `d43b1a5`. The library facts come
from installing testcontainers 4.15.0 in a throwaway venv, reading its source, and running a real
smoke test against local Docker. Items marked *unverified* were not checked first-hand.

## R1. Provision lazily through fixtures, not at collection

**Finding**

- Every integration test reads its address into a module constant at import
  (`URI = os.environ.get("MILVUS_TEST_URI")`) and skips through a collection-time
  `pytestmark = skipif(...)`. For example `test_index_stamp_milvus.py:25-30` and
  `test_maintenance_lock_pg.py:23`.
- Adapters are imported lazily inside fixtures.

**Decision**

- A new `tests/integration/conftest.py` provides one session-scoped fixture per service:
  `postgres_url`, `milvus_uri` and `neo4j_conn`.
- Each fixture resolves in order:
  1. the explicit variable, if set (FR-007);
  2. otherwise, if the switch is on, it starts the container and returns its address;
  3. otherwise it calls `pytest.skip` with a reason naming both options (FR-008).
- Each test drops its module constant and `skipif`, and takes the fixture instead (FR-010).
- pytest only instantiates the fixtures a selected test requests. So a Postgres-only run starts
  only Postgres, and each service starts at most once per session (FR-004).

**Alternatives**

- Provisioning in `pytest_configure` and exporting the variables before modules import. That keeps
  the tests unchanged, but it starts every service even for a subset. Doing it selectively needs
  `pytest_collection_modifyitems` bookkeeping, which is more complex and easier to get wrong.
- Keeping `skipif` and adding a fixture. The collection-time skip can't see a container address
  that doesn't exist yet.

## R2. The opt-in switch

**Decision**: `TREEWEFT_ITEST_CONTAINERS=1`. Constitution II requires integration tests to skip
unless explicitly opted in. The switch is that opt-in for the self-provisioned mode, and the
per-service variables remain the opt-in for the explicit mode. The unit suite never reads it
(`testpaths = ["tests/unit"]`, `pyproject.toml:99`), and `tests/unit` has no fixture that
provisions (FR-009).

## R3. Library: testcontainers 4.15, community modules

**Decision**

- Use `testcontainers[postgres,neo4j,milvus]>=4.15,<4.16` in both dev dependency lists
  (`[project.optional-dependencies] dev`, which CI installs, and `[dependency-groups] dev`), then
  run `uv lock`.
- Import from `testcontainers.community.{postgres,neo4j,milvus}`. The top-level module paths emit
  DeprecationWarnings.
- It adds `docker` 7.x to the lock. Its `neo4j` extra resolves against the repo's neo4j 6.2 pin.
- Pin to the minor version because of R5: the loopback binding relies on the `ports` attribute,
  which is not a documented API.

**Facts**

- Python ≥3.10, so it covers CI's 3.11 and 3.14.
- Ready-made modules exist for all three services.
- Smoke test with images cached:

  | Service | Image | Healthy in |
  |---|---|---|
  | Postgres | `pgvector/pgvector:pg16` | 4.3 s |
  | Milvus standalone | `milvusdb/milvus:v2.5.4` | 4.2 s |
  | Neo4j | `neo4j:5-community` | 8.3 s |

- `MilvusContainer` already runs `milvus run standalone` with embedded etcd and local storage. It
  waits on the "Welcome to use Milvus!" log line and `GET :9091/healthz`.

**Alternatives**

- Driving `docker compose` from pytest. That shares state with a developer's compose project names,
  has no crash reaper, and ports are fixed unless templated.
- Hand-rolled `docker run` in fixtures. That re-implements waits, random ports and cleanup.

## R4. One source of truth for image versions

**Decision**: the conftest reads `services.{postgres,milvus,neo4j}.image` from `docker-compose.yml`
with PyYAML (FR-002). A unit test asserts that the resolver returns exactly the compose images and
fails, naming the service, if one is missing. PyYAML is already a direct dependency (`pyproject.toml:41`), so nothing is added.

**Alternatives**: a Python constants module. Compose can't read Python, so the two would drift;
that is exactly what FR-002 forbids.

## R5. Loopback-only, random ports

**Finding**: `with_exposed_ports` leaves `ports[p] = None`, and docker-py then publishes on
**0.0.0.0**. `with_bind_ports` takes no IP.

**Decision**

- After constructing each container, set `container.ports[p] = ("127.0.0.1", None)` for every
  exposed port. The host port stays random; the binding is loopback only. This was verified with
  `docker inspect`, for example `{'5432/tcp': [('127.0.0.1','32768')]}`.
- The helper is a pure function over the ports mapping, so it is unit-testable without Docker.
  Constructing a `DockerContainer` needs Docker.
- An integration self-test (R10) asserts the binding on the real containers.

## R6. Credentials: explicit, generated, never inherited

**Finding**

- `PostgresContainer` defaults its user, password and database from `POSTGRES_USER`,
  `POSTGRES_PASSWORD` and `POSTGRES_DB` if those are set.
- `Neo4jContainer` defaults its password from `NEO4J_PASSWORD`, which is also the app's variable,
  and the repo `.env` fills it in.
- The Neo4j graph store reads `NEO4J_URI`, `NEO4J_USER` and `NEO4J_PASSWORD` into module constants
  at import (`adapters/neo4j/graph_store.py:27-29`).
- `treeweft/__init__.py` loads `.env` with `setdefault`, so on a developer machine every unset
  variable takes the real local stack's value.

**Decision**

- Fixtures pass explicit per-run values: user `itest`, a random `secrets.token_hex` password and
  database `itest` for Postgres, and a random password for Neo4j.
- The Neo4j fixture gives tests a connection object `{uri, user, password}`. The Neo4j tests
  **monkeypatch the graph store's module attributes** (`NEO4J_URI`, `NEO4J_USER`,
  `NEO4J_PASSWORD`, `_driver = None`), not just the environment. This matters because a module
  imported earlier in the session ignores environment changes (FR-003).
- No fixture reads `.env` for an address in this mode.

## R7. Failing loudly (FR-006)

**Decision**

- A session fixture `docker_available` (depended on by the three service fixtures only in
  self-provisioned mode) pings Docker with `docker.from_env().ping()`.
- If `docker.errors.DockerException` is raised, it calls `pytest.fail` with the message "Docker is
  required when TREEWEFT_ITEST_CONTAINERS=1: <error>".
- A `start()` failure (for example, a pull error) is re-raised as `pytest.fail` naming the image
  and error.
- A wait timeout fails with the container's last 200 log lines in the message.
- Waits use the modules' own strategies, each with an explicit startup timeout: Milvus 180 s,
  Neo4j 120 s, Postgres 60 s. These override the global 120 s so a cold CI start isn't cut short.

## R8. Cleanup (FR-005)

**Decision**

- Normal exit: the session fixture's teardown calls `container.stop()`.
- Crash or interrupt: rely on Ryuk (on by default). It holds a socket and removes the session's
  labelled containers within `RYUK_RECONNECTION_TIMEOUT` (10 s) after the process dies. No
  containers were left behind after the smoke test.
- Rootless Docker or Docker Desktop may need `TESTCONTAINERS_RYUK_PRIVILEGED` or
  `TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE`. This is documented, not automated (*unverified*
  first-hand).

## R9. Service logs for failures (FR-013)

Ryuk and teardown remove the containers before a CI step could read them.

**Decision**

- In each service fixture's teardown, if the session had failures (`request.session.testsfailed`),
  write the container's logs to `.itest-logs/<service>.log` before stopping it.
- The CI job uploads `.itest-logs/` as an artifact on failure. `.itest-logs/` is added to
  `.gitignore`.

## R10. Integration self-test

**Decision**: add `tests/integration/test_provisioning.py`. It uses the fixtures and asserts:

- each provisioned service is published on 127.0.0.1 only (`docker inspect` through the
  testcontainers container object);
- its image tag equals the compose pin;
- the credentials are the generated ones, not `.env`'s.

Only this file inspects the containers. It skips in explicit mode, where there is nothing to
inspect.

## R11. CI job (FR-011–FR-014)

**Decision**: a new `integration` job in `.github/workflows/ci.yml`, patterned on `unit-tests`.

- `needs: changes`, `if: needs.changes.outputs.code == 'true'`, and the **static name**
  `integration`.
- `runs-on: ubuntu-latest`, which has Docker.
- The same `setup-uv` step (pinned commit, cache), `uv sync --locked --extra dev --extra
  all-backends --extra simple`, and `cp .env.example .env`, which supplies the import-time
  variables.
- Then `TREEWEFT_ITEST_CONTAINERS=1 uv run --locked --no-sync pytest tests/integration -m slow -q`.
- On failure, upload the `.itest-logs` artifact.
- It is not added to the repository ruleset, so it is advisory (FR-014). The maintainer adds it
  there later.

*Unverified*: the cold-pull time on GitHub runners. It is estimated at a few minutes; image caching
is left as an optimisation.

## R12. Milvus settings missing from the module

**Finding**: `MilvusContainer` does not set compose's `security_opt: seccomp:unconfined` or the
`embedEtcd.yaml` mount. It started healthy without them on this host.

**Decision**: leave them out unless the first CI run proves one is needed. If so, add them in one
`with_kwargs(...)` call, because `with_kwargs` replaces rather than merges.

## R13. Scope of test changes (FR-010)

**Decision**: change only how each test gets its address.

| File | Change |
|---|---|
| `test_index_stamp_milvus.py`, `test_milvus_filter_injection.py` | take `milvus_uri`. The adapter keeps being built with explicit host, port and `a.uri`. |
| `test_maintenance_lock_pg.py` | takes `postgres_url` |
| `test_index_stamp_neo4j.py`, `test_neo4j_orphan_cleanup.py` | take `neo4j_conn` and monkeypatch the graph store per R6 |

The fixed collection name in `test_milvus_filter_injection.py` is fine, because each run gets its
own Milvus. The ADR-003 tests on branch 002 are converted by whichever feature merges second.

## R14. Docs (FR-015)

- `CLAUDE.md`, Tests section: the integration command in both modes.
- `CONTRIBUTING.md`: a short "Integration tests" section.
- `docs/engineering-notes.md`: the details:
  - the switch and the per-service variables, and their precedence;
  - prerequisites: Docker, and about 2 GB of images;
  - running a subset;
  - the Ryuk overrides for rootless Docker or Docker Desktop;
  - where CI logs go.
