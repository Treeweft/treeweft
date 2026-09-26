# Quickstart: Validating Self-Provisioned Integration Services

The fixture contract is in [contracts/fixtures.md](contracts/fixtures.md).

## 1. Unit suite (no Docker)

```bash
env -u PYTHONPATH .venv/bin/python -m pytest tests/unit -q
```

It must pass, including `tests/unit/test_integration_services.py`:
- `compose_image` returns the compose pins, and fails naming the service when a service is missing;
- `bind_loopback` binds every port to `127.0.0.1` with a random host port;
- `resolve_mode` gives the explicit variable precedence over the switch, which takes precedence
  over skip;
- the skip reason names both options.

Confirm the unit suite never needs Docker: run it with `DOCKER_HOST=unix:///nonexistent.sock`. It
must pass unchanged (SC-005).

## 2. Self-provisioned run

```bash
TREEWEFT_ITEST_CONTAINERS=1 env -u PYTHONPATH .venv/bin/python -m pytest tests/integration -m slow -q
```

- Every integration test runs, none are skipped, and the run takes under 5 minutes with images
  cached (SC-001).
- `test_provisioning.py` passes: loopback-only bindings, compose image tags, generated credentials.
- Afterwards, `docker ps -a --filter label=org.testcontainers` shows nothing, apart from a Ryuk
  container that is still exiting (SC-002).

## 3. The real stack is untouched (SC-002)

With a local stack running on the default ports (Postgres 5432, Milvus 19530, Neo4j 7687), record
before and after the run in section 2:
- Milvus: `list_collections()` and each collection's row count;
- Postgres: the table count and row counts for `jobs` and `source_records`;
- Neo4j: the node count.

All must be identical.

## 4. Subset and explicit modes

- `TREEWEFT_ITEST_CONTAINERS=1 pytest tests/integration -m slow -k maintenance_lock` starts only
  Postgres. Check with `docker ps` while it runs, or with the fixture's log line.
- `POSTGRES_TEST_URL=<a URL> pytest tests/integration -m slow -k maintenance_lock`, with the switch
  off, uses that URL and starts nothing.
- With neither set: the tests skip with the two-option reason.

## 5. Failure paths (FR-006)

- `TREEWEFT_ITEST_CONTAINERS=1 DOCKER_HOST=unix:///nonexistent.sock pytest tests/integration -m
  slow` fails with "Docker is required …". It does not skip.
- Temporarily point `compose_image` at a non-existent tag, for example through a monkeypatched
  compose path in a scratch copy. The run fails naming the image. Restore it afterwards.

## 6. CI (SC-003, SC-004)

- On this feature's pull request, the `integration` check runs and passes. It is not required
  (FR-014).
- A documentation-only commit shows `integration` as skipped/passed.
- Regression proof, run locally so nothing broken is pushed: temporarily break `_escape_literal`
  in the Milvus adapter and run the self-provisioned suite. `test_milvus_filter_injection.py` must
  fail. Revert. CI runs the same command, so a red local run means a red check (SC-004).
