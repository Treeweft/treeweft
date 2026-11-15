# Contract: credential refusal (409)

Applies to the six gated routes (`/index-file`, `/index-directory`, `/index-repo`, `/index-graph`,
job retry, `POST /index/rebuild`). The push webhook is exempt (FR-005).

```json
{
  "detail": "Embedding backend rejected its credential (HTTP 401): api.openai.com, model text-embedding-3-small. Check EMBEDDING_API_KEY / OPENAI_API_KEY.",
  "reason": "embedding_auth_failed",
  "backends": [
    {"role": "embedding", "host": "api.openai.com", "model": "text-embedding-3-small",
     "state": "auth_failed", "setting": "EMBEDDING_API_KEY"}
  ]
}
```

Rules:
- `reason` is one of `embedding_auth_failed`, `summarizer_auth_failed`; when both fail, `reason`
  is `embedding_auth_failed` and `backends` lists both.
- When two roles share a key, one entry lists both in `roles`, replacing `role` (FR-006a).
- `setting` is a variable *name*, never a value. The body, `detail` and logs never contain the key
  or any prefix of it (FR-012).
- Status stays 409, matching `parsers_unavailable` and the index-stamp refusals; no new status
  code, so existing clients' conflict handling applies.
- Job error text for a failed job uses the same code as a prefix: `embedding_auth_failed: ...`.
