# Quickstart: verifying credential pre-flight

Run from the repo root with the CI-style `.env` (`cp .env.example .env`).

## Unit checks (no real provider)

```bash
env -u PYTHONPATH .venv/bin/python -I -m pytest -q \
  tests/unit/test_credential_classification.py \
  tests/unit/test_credential_preflight.py \
  tests/unit/test_credential_failfast.py \
  tests/unit/test_health_credentials.py
```

The incident regression in `test_credential_failfast.py` (every embeddings call answers 401 over a
many-file repo) must fail on `main` and pass on this branch; show both runs before trusting it.

## Manual check against a deliberately wrong key (simple mode)

```bash
OPENAI_API_KEY=sk-wrong ./run-simple.sh        # then, in another shell:
curl -s localhost:8001/health | jq .credentials
curl -s -X POST localhost:8001/index-repo -H 'content-type: application/json' \
  -d '{"path": "/path/to/small/repo"}' -w '\n%{http_code}\n'
```

Expect: `/health` shows `embedding` `auth_failed`; `/index-repo` answers `409` with
`reason: embedding_auth_failed`, no job is listed in `GET /jobs`, and neither the response nor
`journalctl`/the console contains any part of the key.

## Fail-fast check

Start a job with a good key, then swap to a bad one mid-run (or point at a stub server that begins
returning 401 after N calls). Expect the job to end `failed` with `embedding_auth_failed` within
`TREEWEFT_CREDENTIAL_FAIL_STREAK` further failures, and `POST /jobs/{id}/retry` to resume once the
key is fixed.

## Contracts

```bash
python scripts/update_contracts.py && git diff --stat contracts/
```

Only additive `credentials` and 409-reason changes should appear.
