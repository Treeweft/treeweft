# Data Model: Backend Credential Pre-flight

No persisted entity and no migration. Everything is process-local and derived; the only durable
effect is the existing job record gaining a failure reason.

## CredentialState (enum)

`valid` | `auth_failed` | `unreachable`. Exactly one per check (FR-002). Only `auth_failed` blocks.

## BackendStatus (in memory, per distinct endpoint+key)

| Field | Meaning |
|---|---|
| `roles` | Set of `embedding` / `summarizer` that share this endpoint and key (FR-006a) |
| `state` | `CredentialState` |
| `checked_at` | Monotonic time of the probe, with a wall-clock time for display |
| `host`, `model` | For the refusal body and job error only; never in `/health` (R7) |
| `reason_code` | Stable code, e.g. `embedding_auth_failed`, `summarizer_auth_failed` |
| cache key | `(endpoint, sha256(key))`, memory only, never emitted (R4, FR-012) |

Lifetime: `valid` for the check TTL, anything else for the shorter recheck interval. A new request
after expiry re-probes; concurrent requests share one probe.

## CredentialFailureStreak (per running job)

| Field | Meaning |
|---|---|
| `consecutive` | Count of consecutive `BackendAuthError`s; reset to 0 by any successful embed |
| `aborted` | Set once `consecutive >= TREEWEFT_CREDENTIAL_FAIL_STREAK`; later files skip the embedder |

Lives on the job dict for the life of the run. Not persisted separately: the job's terminal
`error` text (`embedding_auth_failed: embedding <host>`) is what survives, so a retry starts clean.

## State transitions

```text
probe result:  (none) -> valid | auth_failed | unreachable
               valid --(TTL)--> re-probe       auth_failed --(recheck)--> re-probe
job streak:    0 -> n (each BackendAuthError) -> reset on success
               n == threshold -> aborted -> job failed (retryable)
```
