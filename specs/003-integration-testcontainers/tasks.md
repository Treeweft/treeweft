---
description: "Task list for 003-integration-testcontainers"
---

# Tasks: Self-Provisioned Integration Test Services

**Input**: Design documents from `specs/003-integration-testcontainers/`: [plan.md](plan.md),
[spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md),
[contracts/fixtures.md](contracts/fixtures.md), [quickstart.md](quickstart.md).

**Tests**: REQUIRED (constitution II). In each phase the test tasks come first and MUST fail
before the implementation task that follows them is started.

**Commands** (always the project venv):
- unit: `env -u PYTHONPATH .venv/bin/python -m pytest tests/unit -q`;
- integration: `TREEWEFT_ITEST_CONTAINERS=1 env -u PYTHONPATH .venv/bin/python -m pytest
  tests/integration -m slow -q`.

A real local stack may be running on the default ports. In self-provisioned mode no task may
connect to it.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US3 from spec.md

---

## Phase 1: Setup

- [X] T001 Add `testcontainers[postgres,neo4j,milvus]>=4.15,<4.16` to both
  `[project.optional-dependencies] dev` and `[dependency-groups] dev` in `pyproject.toml`, then
  run `uv lock` (research R3).
  - Confirm that `uv sync --locked --extra dev --extra all-backends --extra simple` succeeds, and
    that the resolved `neo4j` is still the repo's pin.
  - Add `.itest-logs/` to `.gitignore` (R9).
  - The Nexus PyPI proxy can exceed pip's timeout on large wheels. If a lock or sync times out, say
    so rather than working around it.

---

## Phase 2: Foundational (blocking prerequisites)

**Purpose**: the pure helpers and the fixtures every story uses.

- [X] T002 [P] Write `tests/unit/test_integration_services.py` for `tests/integration/_services.py`
  ([contracts/fixtures.md](contracts/fixtures.md) "Helpers"). It must not import testcontainers'
  container classes and must pass with `DOCKER_HOST=unix:///nonexistent.sock`.
  - `compose_image(service)` returns `pgvector/pgvector:pg16`, `milvusdb/milvus:v2.5.4` and
    `neo4j:5-community` from the real `docker-compose.yml`.
  - On a temporary compose file missing a service, `compose_image` raises an error naming the
    service.
  - `bind_loopback({5432: None, 9091: None})` returns every port bound to `("127.0.0.1", None)`.
  - `resolve_mode`:
    - the explicit variable wins over the switch, for each service;
    - the switch without the variable gives `container`;
    - neither gives `skip`;
    - only `TREEWEFT_ITEST_CONTAINERS == "1"` counts as on.
  - The skip reason for each service names both its variable and `TREEWEFT_ITEST_CONTAINERS=1`.
- [X] T003 Implement `tests/integration/_services.py`: `compose_image`, `bind_loopback`,
  `resolve_mode` and the per-service skip reasons. Parse the compose file with PyYAML.
- [X] T004 Implement `tests/integration/conftest.py` per
  [contracts/fixtures.md](contracts/fixtures.md), research R5–R9, and R12.
  - **Fixtures**: session-scoped, synchronous `docker_available`, `postgres_url`, `milvus_uri` and
    `neo4j_conn` (`Neo4jConn`), plus the function-scoped `neo4j_graph_store`.
  - **Resolution**: explicit variable, else container (after `docker_available`), else
    `pytest.skip(reason)`.
  - **Containers**:
    - use `testcontainers.community.{postgres,neo4j,milvus}` with the compose image;
    - apply `bind_loopback` to `container.ports` before `start()`;
    - pass generated credentials explicitly: `PostgresContainer(username="itest",
      password=<token_hex>, dbname="itest", driver=None)` and
      `Neo4jContainer(password=<token_hex>)`;
    - set startup timeouts: Milvus 180 s, Neo4j 120 s, Postgres 60 s;
    - return the loopback address.
  - **Failures**:
    - `DockerException` → `pytest.fail("Docker is required when TREEWEFT_ITEST_CONTAINERS=1:
      …")`;
    - start/pull errors → `pytest.fail` naming the image;
    - wait timeouts → `pytest.fail` with the container's last 200 log lines.
  - **Teardown**: if `request.session.testsfailed`, write the logs to
    `.itest-logs/<service>.log`, then `stop()`.
  - **`neo4j_graph_store`**: monkeypatch `graph_store.NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`,
    reset `_driver` before and after, and close the driver at teardown.
  - **No `.env`**: nothing in this mode reads `.env` for an address or credential.

**Checkpoint**: `tests/unit` is green and needs no Docker.

---

## Phase 3: User Story 1 — A developer runs the whole integration suite with one command (Priority: P1) 🎯 MVP

**Goal**: with the switch on, every integration test runs against throwaway services and leaves
nothing behind.

**Independent Test**: with the real local stack running, run the integration command. Zero tests
skip, the real stack is unchanged, and no test containers remain (quickstart §2–§3).

### Tests for User Story 1 (write first)

- [X] T005 [US1] Write `tests/integration/test_provisioning.py` (`slow`; skips unless the fixture
  resolved to `container`, R10). For each service it requests:
  - the running container publishes only on `127.0.0.1` (read the port bindings from the Docker
    API through the container object);
  - its image equals `compose_image(service)`;
  - the Postgres URL's user is `itest` and its password is not `.env`'s `POSTGRES_PASSWORD`;
  - the Neo4j password is not `.env`'s `NEO4J_PASSWORD`.

  Expose the container objects to the test through a small registry in the conftest, not through
  module globals the tests reach into.

### Implementation for User Story 1

- [X] T006 [P] [US1] Convert `tests/integration/test_index_stamp_milvus.py` and
  `tests/integration/test_milvus_filter_injection.py`:
  - remove the module-level `URI`/`skipif`;
  - take `milvus_uri`;
  - keep building `MilvusAdapter(host, port, collection_name=…, vector_dim=…)` with `a.uri =
    milvus_uri`;
  - make no other behavioural change (FR-010).
- [X] T007 [P] [US1] Convert `tests/integration/test_maintenance_lock_pg.py` to `postgres_url`,
  covering both the pool and the raw `asyncpg.connect` uses.
- [X] T008 [P] [US1] Convert `tests/integration/test_index_stamp_neo4j.py` and
  `tests/integration/test_neo4j_orphan_cleanup.py` to `neo4j_graph_store`. Replace
  `os.environ["NEO4J_URI"] = …` with the fixture's patched module. Keep their `_META_ID` patching,
  run prefixes and scoped teardown.
- [ ] T009 [US1] Run the full self-provisioned suite with the local stack up (quickstart §2–§3).
  Record:
  - pass/skip counts (expect 0 skipped);
  - wall time (SC-001);
  - the real stack's Milvus collections and row counts, Postgres table and row counts, and Neo4j
    node count, before and after (SC-002);
  - `docker ps -a --filter label=org.testcontainers` afterwards.

  Then run one test in isolation (`-k maintenance_lock`) and confirm that only Postgres started
  (FR-004).

**Checkpoint**: US1's acceptance scenarios 1, 2 and 4 are verified. Scenario 3 (crash) is covered
in T010.

- [ ] T010 [US1] Crash cleanup (FR-005): start the suite in self-provisioned mode, kill the pytest
  process (SIGKILL) once the containers are up, and wait 15 s. Confirm that no
  `org.testcontainers`-labelled containers remain. Record the observed time.

---

## Phase 4: User Story 3 — Developers can still run against services they manage (Priority: P2)

**Goal**: explicit variables keep working and take precedence; with nothing set, tests skip with a
clear reason.

**Independent Test**: with the switch off and only `POSTGRES_TEST_URL` set, the Postgres tests run
against it and the rest skip with two-option reasons.

- [ ] T011 [US3] Verify explicit mode (quickstart §4). Start a throwaway Postgres by hand on a free
  loopback port, run `-k maintenance_lock` with `POSTGRES_TEST_URL` set and the switch off, and
  confirm the test ran against it with no container started by the suite. Then run with the
  switch on as well, and confirm the explicit URL still wins (FR-007). With nothing set, confirm
  every integration test skips and its reason names both options (FR-008). Remove the hand-started
  container.
- [ ] T012 [US3] Verify the failure paths (quickstart §5):
  - the switch on with `DOCKER_HOST=unix:///nonexistent.sock` → the run fails with "Docker is
    required", with no skips (FR-006);
  - an unpullable image, simulated by monkeypatching `compose_image` in a scratch run → the run
    fails naming the image.

  Record both outputs.

---

## Phase 5: User Story 2 — CI runs the integration suite on every pull request that changes code (Priority: P1)

**Goal**: an advisory, statically named `integration` check on every code PR, with logs on
failure. It is ordered after US3 only because it needs the finished suite; it has the same
priority as US1.

- [ ] T013 [US2] Add the `integration` job to `.github/workflows/ci.yml` per
  [contracts/fixtures.md](contracts/fixtures.md) "CI job" and research R11:
  - `name: integration` (static), `needs: changes`, `if: needs.changes.outputs.code == 'true'`,
    `runs-on: ubuntu-latest`;
  - the same pinned `setup-uv` step and `uv sync --locked --extra dev --extra all-backends
    --extra simple` as `unit-tests`, then `cp .env.example .env`;
  - `TREEWEFT_ITEST_CONTAINERS=1 uv run --locked --no-sync pytest tests/integration -m slow -q`;
  - `actions/upload-artifact` of `.itest-logs/` with `if: failure()`.

  Pin actions the way the file already does. Update the file's header comment to mention that
  `integration` is advisory (not in the ruleset) until the maintainer promotes it (FR-014). Do not
  touch the ruleset.
- [ ] T014 [US2] Verify on the PR: the `integration` check runs on this feature's PR and passes.
  Record its duration, cold pulls included. If Milvus fails on the runner for seccomp or etcd
  config reasons, apply research R12's single `with_kwargs(...)` fallback, and record why.
  Documentation-only behaviour (FR-012) follows the existing `changes` gating. Verify it from the
  job's `if:` and from how GitHub reports a skipped job, not by pushing a docs-only commit.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [ ] T015 [P] Docs (FR-015, R14):
  - `CLAUDE.md` Tests section: the self-provisioned command, and the note that the explicit
    variables still work;
  - `CONTRIBUTING.md`: a short "Integration tests" section;
  - `docs/engineering-notes.md`:
    - the switch and the variables, and their precedence;
    - prerequisites (Docker, about 2 GB of images);
    - running a subset;
    - the Ryuk overrides (`TESTCONTAINERS_RYUK_PRIVILEGED`, `TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE`)
      for rootless Docker or Docker Desktop;
    - `.itest-logs` and the CI artifact.
- [ ] T016 Regression proof (SC-004, run locally): temporarily break `_escape_literal` in
  `src/treeweft/adapters/milvus/vector_store.py`, run the self-provisioned suite, and confirm that
  `test_milvus_filter_injection.py` fails. Revert, and confirm `git diff` shows no source change.
  Record the failing test names.
- [ ] T017 Final verification:
  - the unit suite (record the count), plus a run with `DOCKER_HOST=unix:///nonexistent.sock`
    (SC-005);
  - the full self-provisioned integration suite (record counts and time);
  - `docker ps -a --filter label=org.testcontainers` clean.

---

## Dependencies & Execution Order

- Setup (T001) → Foundational (T002 → T003; T004 after T001 and T003) → US1 (T005 first; T006,
  T007 and T008 in parallel; then T009 and T010) → US3 (T011, T012) → US2 (T013 → T014) → Polish
  (T015 in parallel with T013; T016 and T017 last).
- T006, T007 and T008 edit different files. T004 is the single conftest, and everything after it
  depends on it.

## Parallel opportunities

```text
Foundational: T002 (tests) alongside T001 (lock)
US1:          T006 | T007 | T008
Polish:       T015 alongside T013
```

## Implementation Strategy

- **MVP**: Setup, Foundational and US1. One command gives a hermetic, self-cleaning integration run
  locally.
- **Then**: US3 (confirm compatibility), then US2 (CI), then Polish. It ships as a single PR.
- **Verification**: T009–T012, T014, T016 and T017 are reported in the PR under constitution I.
- **Branch 002 interplay**: if `002-prompt-versioning` merges first, rebase this branch and convert
  `test_resummarize_milvus.py` and `test_prompt_pins_pg.py` the same way (T006/T007 pattern). If
  this merges first, 002 converts them when it rebases.
