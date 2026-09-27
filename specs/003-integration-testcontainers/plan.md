# Implementation Plan: Self-Provisioned Integration Test Services

**Branch**: `003-integration-testcontainers` | **Date**: 2026-09-26 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/003-integration-testcontainers/spec.md`.

## Summary

The integration suite gains a self-provisioned mode. `TREEWEFT_ITEST_CONTAINERS=1` makes session
fixtures in a new `tests/integration/conftest.py` start throwaway Postgres (pgvector 16), Milvus
2.5.4 standalone and Neo4j 5 containers using testcontainers 4.15.

- **Lazy:** a service starts only when a selected test requests its fixture.
- **Pinned:** images are read from `docker-compose.yml`, so tests and deployment can't drift.
- **Isolated:**
  - every port is bound to `127.0.0.1` on a random host port;
  - credentials are generated per run and never read from `.env`;
  - the Neo4j graph store's module-level connection constants are patched, not just the
    environment.
- **Cleaned up:** containers stop at teardown; Ryuk removes them after a crash.
- **Fails loudly:** a missing Docker, a failed pull or a health timeout fails the run with logs.

The explicit per-service variables keep working and take precedence. Each existing test changes
only how it gets its service address. A new advisory `integration` CI job runs the suite on every
code pull request and uploads service logs on failure. The research and decisions are
R1–R14 in [research.md](research.md).

## Technical Context

**Language/Version**: Python ≥ 3.11 (CI runs 3.11; the library supports ≥3.10).

**Primary Dependencies**: `testcontainers[postgres,neo4j,milvus]>=4.15,<4.16` (new dev
dependency, which also adds `docker` 7.x to the lock); pytest 8, pytest-asyncio (strict mode);
PyYAML (already a direct dependency) to read `docker-compose.yml`.

**Storage**: none. The throwaway containers hold only test data.

**Testing**: `tests/unit/test_integration_services.py` covers the pure helpers and needs no Docker.
`tests/integration/test_provisioning.py` self-tests the provisioned mode. All existing integration
tests run in both modes.

**Target Platform**: developer machines with Docker Engine (amd64 or arm64; all four images are
multi-arch), and GitHub-hosted `ubuntu-latest` runners.

**Project Type**: test infrastructure plus CI for the existing Python service. No product code
changes.

**Performance Goals**:

- Measured with cached images, each service is healthy in 4–9 s. A full self-provisioned suite
  should take under 5 minutes locally (SC-001).
- The cold CI job is dominated by image pulls: a few minutes (*unverified*).

**Constraints**:

- Constitution II:
  - the unit suite never needs Docker;
  - integration tests stay opt-in, marked `slow`, with unique prefixed data and cleanup.
- The CI job's name must be static so it can later become a required check.
- The loopback binding depends on testcontainers' `ports` attribute, hence the minor-version pin
  and a test that asserts the binding.

**Scale/Scope**:

- Existing test files: 5 on `main`, plus 2 on the unmerged 002 branch.
- New files: 1 conftest, 1 helpers module, 2 test files.
- Edits: 1 CI job, 3 docs.

No open clarifications. FR-014 was resolved as advisory in the spec.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design. It passes both times.*

| Principle | How this plan complies |
|---|---|
| I. Verification first | `quickstart.md` fixes the checks: unit helpers, a full self-provisioned run, the real stack left untouched, subset and explicit modes, failure paths, and the CI check. The PR reports each, including anything skipped. |
| II. Test-first, taxonomy | Helpers are unit-tested without Docker. Everything that starts a container lives in `tests/integration`, marked `slow`, opt-in via the switch or explicit variables. Tests keep their unique prefixes and cleanup. A local regression proof shows the suite catching a broken filter escape. |
| III. Evidence-gated retrieval | Not applicable: no retrieval, ranking or model change. |
| IV. Layering | No product code changes, so layering is unaffected. Test helpers import adapters only as the tests already do. |
| V. Fail loud | A missing Docker, a failed pull or a health timeout fails the run with a named cause and the logs. There is never a silent skip in self-provisioned mode, and no fallback to a default address. |
| VI. Secure by default | Containers bind to loopback only, with random ports and per-run generated credentials. No credentials are read from `.env`. Ryuk mounts the Docker socket, which is the library's documented default; this is noted in the docs. |
| VII. Versioned data | No API, MCP, index-schema or Postgres-schema change, so no version bump. The image versions come from the deployment configuration. |
| Workflow gates | No ADR is needed: this is test infrastructure, not an architectural decision. The docs are updated in the same PR. The new CI check stays advisory until the maintainer promotes it (FR-014). |

## Project Structure

### Documentation (this feature)

```text
specs/003-integration-testcontainers/
├── plan.md
├── research.md          # R1–R14
├── data-model.md        # configuration entities (no stored data)
├── quickstart.md
├── contracts/fixtures.md
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks
```

### Source code

```text
tests/integration/
├── _services.py                 # NEW pure helpers: compose_image, bind_loopback, resolve_mode, skip reasons
├── conftest.py                  # NEW session fixtures: docker_available, postgres_url, milvus_uri, neo4j_conn, neo4j_graph_store; failure-log capture
├── test_provisioning.py         # NEW self-test: loopback bindings, compose tags, generated credentials
├── test_index_stamp_milvus.py   # take milvus_uri
├── test_milvus_filter_injection.py
├── test_maintenance_lock_pg.py  # take postgres_url
├── test_index_stamp_neo4j.py    # take neo4j_graph_store
└── test_neo4j_orphan_cleanup.py
tests/unit/test_integration_services.py   # NEW helper tests (no Docker)
pyproject.toml, uv.lock                   # testcontainers dev dependency
.github/workflows/ci.yml                  # NEW advisory `integration` job
.gitignore                                # .itest-logs/
CLAUDE.md, CONTRIBUTING.md, docs/engineering-notes.md
```

**Structure Decision**: keep everything inside the existing `tests/integration` tree. The helpers
sit in a private module so they are unit-testable without importing testcontainers' container
classes, which need Docker to construct.

### Suggested delivery order (input to `/speckit-tasks`)

1. **Foundation**: the dependency, the pure helpers and their unit tests, and the conftest
   fixtures, which are US1's core.
2. **US1**: convert the five existing tests, add the provisioning self-test, and do the full local
   run plus the real-stack-untouched check.
3. **US3**: explicit mode precedence and skip reasons. This is mostly covered by the helpers; add
   the verification.
4. **US2**: the CI job with the log artifact.
5. **Polish**: docs, and the regression proof.

## Complexity Tracking

No constitution violations to justify.
