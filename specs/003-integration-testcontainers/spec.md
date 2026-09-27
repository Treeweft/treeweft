# Feature Specification: Self-Provisioned Integration Test Services

**Feature Branch**: `003-integration-testcontainers`

**Created**: 2026-09-26

**Status**: Draft

**Input**: User description: "should we start using testcontainers for this? https://testcontainers.com/"
— "yes, do it with speckit-specify". Context: running the ADR-003 integration and end-to-end
checks needed hand-started throwaway Postgres and Milvus containers, and one attempt wrote test
data into the developer's real Milvus because a connection setting pointed at the wrong port.

## Background

The integration tests in `tests/integration/` exercise real Milvus, Postgres and Neo4j. Today each
test reads its own opt-in variable (`MILVUS_TEST_URI`, `POSTGRES_TEST_URL`, `NEO4J_TEST_URI`) and
skips when it is unset. A developer has to start the right service versions by hand and point the
variables at them, so the suite is rarely run. CI never runs it. And because the variables can
point anywhere, a mistake can aim the suite at real data.

This feature lets the integration suite provision its own throwaway services: the same versions
production uses, on isolated ports, removed afterwards. CI runs the suite on every relevant pull
request. The explicit-URI mode keeps working for anyone who wants to run against services they
manage themselves.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A developer runs the whole integration suite with one command (Priority: P1)

A developer with Docker available sets one opt-in switch and runs the integration suite. The
suite starts throwaway Postgres, Milvus and Neo4j services at the versions production uses, runs
every integration test against them, and removes them afterwards. Nothing on the developer's
machine outside those throwaway services is read or written, even if a real stack is running
locally on the default ports.

**Why this priority**: This is the core value. It turns a manual, error-prone setup into one
command, and makes a leak into real data structurally impossible.

**Independent Test**: With a local stack running on the default ports, enable the switch and
run the suite. Every integration test that was previously skipped for lack of a service runs and
passes; the real stack's databases, collections and graph are unchanged afterwards; no test
containers remain.

**Acceptance Scenarios**:

1. **Given** Docker is available and the switch is on, **When** the developer runs the
   integration suite, **Then** throwaway Postgres, Milvus and Neo4j services are started once for
   the run, every integration test runs against them, and they are removed when the run ends.
2. **Given** a real stack is listening on the default service ports, **When** the suite runs with
   the switch on, **Then** the tests connect only to the throwaway services, and the real stack's
   data is unchanged.
3. **Given** the switch is on, **When** the run is interrupted or crashes, **Then** no throwaway
   service outlives the run for more than a short grace period.
4. **Given** the switch is on, **When** the suite runs, **Then** each service is the same version
   production uses (the versions in the deployment configuration), and a version change there is
   reflected here without editing the tests.
5. **Given** the switch is on but Docker is not available, **When** the suite runs, **Then** it
   fails with a message saying Docker is required, rather than skipping silently.

---

### User Story 2 - CI runs the integration suite on every pull request that changes code (Priority: P1)

Every pull request that changes code runs the integration suite against self-provisioned services
and reports the result as a check, next to the unit tests.

**Why this priority**: Real-service behaviour (query semantics, consistency, schema behaviour) is
only proven against real services (constitution II), and today nothing checks it automatically.
The ADR-003 work showed that bugs in exactly this area reach review.

**Independent Test**: Open a pull request that changes a source file; the integration check runs
and reports. Open one that changes only documentation; the check reports without running the
suite, as the existing unit-test check does.

**Acceptance Scenarios**:

1. **Given** a pull request that changes code, **When** CI runs, **Then** an integration check
   runs the whole suite against self-provisioned services and reports pass or fail.
2. **Given** a pull request that changes only documents or images, **When** CI runs, **Then** the
   integration check reports without running the suite, so it never blocks a documentation-only
   change.
3. **Given** an integration test fails in CI, **When** a developer opens the check, **Then** the
   failing test and its output are visible, including the relevant service logs.
4. **Given** the integration check, **When** a pull request is evaluated for merging, **Then** the
   check's result counts as FR-014 specifies.

---

### User Story 3 - Developers can still run against services they manage (Priority: P2)

A developer who already runs services (for example a shared test database) can keep pointing the
suite at them with the existing variables, as today.

**Why this priority**: The existing mode is used and costs nothing to keep; removing it would
break current workflows. The new mode is the default recommendation, not the only option.

**Independent Test**: With the switch off and `POSTGRES_TEST_URL` set, the Postgres integration
tests run against that database and the others skip, exactly as today.

**Acceptance Scenarios**:

1. **Given** a service's explicit variable is set, **When** the suite runs, **Then** that service's
   tests use the given address, whether or not the switch is on (explicit configuration wins), and
   no throwaway instance of that service is started.
2. **Given** neither the switch nor a service's variable is set, **When** the suite runs, **Then**
   that service's tests skip with a reason naming both ways to enable them.
3. **Given** the unit suite, **When** it runs, **Then** it never starts a container and never needs
   Docker (constitution II).

### Edge Cases

- **Slow service start.** Milvus can take tens of seconds to become healthy. Each service is
  started at most once per run and shared by the tests that need it; a test waits for its
  service's health check, with a bounded timeout that fails the run with the service's logs when
  exceeded.
- **Only some services needed.** Running a subset of tests (for example only the Postgres ones)
  starts only the services those tests need.
- **Isolation between tests sharing a service.** Tests already use uniquely prefixed data and clean
  up after themselves (constitution II); sharing one service per run keeps that rule.
- **Parallel runs on one machine** (two developers' suites, or a developer plus a local CI
  runner). Each run gets its own services on its own ports and does not see the other's data.
- **Docker available but the image cannot be pulled** (offline, registry rate limit). The run fails
  naming the image and the pull error, rather than skipping.
- **Container runtime other than Docker Desktop / Docker Engine** (for example Podman). Out of
  scope; a clear failure message is enough.
- **Architecture** (arm64 developer machines). The pinned images must run there, or the failure
  must say which image lacks the architecture.

## Requirements *(mandatory)*

### Functional Requirements

**Provisioning**

- **FR-001**: The integration suite MUST be able to start throwaway Postgres, Milvus and Neo4j
  services itself when an explicit opt-in switch is set, run against them, and remove them
  afterwards.
- **FR-002**: Each service MUST use the version the deployment configuration pins for production
  (today Postgres with pgvector 16, Milvus 2.5.4 standalone, Neo4j 5 community), read from one
  place so the two cannot drift.
- **FR-003**: Each service MUST listen only on the local interface, on a port chosen at run time,
  and the tests MUST connect only through the address the suite provisioned. No test may fall back
  to a default address or read the repository `.env` for a service address in this mode.
- **FR-004**: A service MUST be started at most once per run, only if a selected test needs it,
  and MUST be reported healthy before any test uses it. Exceeding the start timeout MUST fail the
  run and show the service's logs.
- **FR-005**: Services MUST be removed when the run ends, including after a crash or interruption
  (within a short grace period).
- **FR-006**: With the switch on and Docker unavailable, or an image that cannot be pulled, the run
  MUST fail with a message naming the cause (constitution V).

**Compatibility**

- **FR-007**: An explicit per-service variable (`POSTGRES_TEST_URL`, `MILVUS_TEST_URI`,
  `NEO4J_TEST_URI` and its companions) MUST take precedence over provisioning for that service.
- **FR-008**: With neither the switch nor a service's variable set, that service's tests MUST skip
  with a reason that names both ways to enable them.
- **FR-009**: The unit suite MUST NOT start containers or require Docker. Integration tests stay
  under `tests/integration/`, marked `slow` (constitution II).
- **FR-010**: Every existing integration test MUST run unchanged in intent under the new mode;
  changes are limited to how a test obtains its service address.

**CI**

- **FR-011**: CI MUST run the integration suite with self-provisioned services on every pull
  request that changes code and on pushes to `main`, as a named check.
- **FR-012**: On a documentation- or image-only change, the integration check MUST report without
  running the suite, following the existing CI pattern, so it never blocks such a change.
- **FR-013**: A failing integration check MUST show the failing tests and the services' logs.
- **FR-014**: The integration check MUST be advisory at first: it reports on every code pull
  request but is not a required status check. The maintainer adds it to the required checks in
  the repository ruleset once it has proven stable. Its job MUST have a static name so it can be
  made required later without changes (the existing CI constraint).

**Docs**

- **FR-015**: The developer docs MUST describe both modes (self-provisioned and explicit
  addresses), the prerequisites (Docker, disk and time for the images), and how to run a subset.
  `CLAUDE.md`'s test section MUST mention the integration command.

### Key Entities

- **Opt-in switch**: one variable that enables self-provisioning for the run.
- **Service definition**: for each of Postgres, Milvus and Neo4j, the pinned image and version, the
  health check, and the connection details it exposes to tests.
- **Service address**: what a test receives — the provisioned address, or the explicit one from
  its variable.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On a machine with Docker and the images already pulled, one command runs the whole
  integration suite with zero skipped tests, zero manual service setup, and in under 5 minutes.
- **SC-002**: With a real stack running on the default ports, a full self-provisioned run leaves
  that stack's data byte-for-byte unchanged (verified by listing its collections, tables and graph
  counts before and after), and leaves zero test containers behind.
- **SC-003**: Every pull request that changes code shows an integration check result, and a
  documentation-only pull request shows the check as passed without running it.
- **SC-004**: A deliberately broken real-service behaviour (for example a filter-escaping
  regression covered by an existing integration test) turns the CI integration check red on the
  pull request that introduces it.
- **SC-005**: The unit suite's run time and its independence from Docker are unchanged.

## Assumptions

- The implementation uses the Testcontainers library for Python, which the user proposed. Where it
  lacks a ready-made module for a service (Milvus standalone), a generic container definition with
  a health check is acceptable.
- GitHub-hosted Ubuntu runners provide Docker, which is enough for CI; no self-hosted runner is
  needed.
- The CI integration job may take several minutes, mostly image pulls and Milvus start-up; caching
  images is an optimisation, not a requirement.
- This changes only tests, CI and docs. It changes no product behaviour, HTTP API, MCP surface or
  index schema, so it needs no version bump and no changelog entry beyond an internal note.
- The ADR-003 integration tests (`test_resummarize_milvus.py`, `test_prompt_pins_pg.py`) live on
  the unmerged `002-prompt-versioning` branch. Whichever of the two features merges second adapts
  them to the new mode.
- Neo4j needs credentials; the provisioned instance uses test-only credentials generated for the
  run, never ones from `.env`.
