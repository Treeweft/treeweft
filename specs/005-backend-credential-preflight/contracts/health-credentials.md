# Contract: `/health` additions

`GET /health` (public) gains an additive object. Existing fields are unchanged, so this is a
SemVer MINOR.

```json
{
  "credentials": {
    "ready_for_indexing": false,
    "backends": [
      {"role": "embedding", "state": "auth_failed", "checked_at": "2026-11-15T23:41:07Z"},
      {"role": "summarizer", "state": "skipped", "checked_at": null}
    ]
  }
}
```

Rules:
- `state` is `valid`, `auth_failed`, `unreachable`, or `skipped` (summarizer with summaries off,
  or a key-free embedder). `skipped` is never an error.
- Public endpoint: no host, model, reason text or key material (R7, Principle VI).
- `ready_for_indexing` is false only while a *required* role is `auth_failed`; `unreachable` does
  not clear it, so a provider outage does not read as "bad key".
- Reads the cached view; `/health` never triggers a probe.
