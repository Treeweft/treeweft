"""Backend credential state: classification, scrubbing and the failure streak.

Pure logic, no I/O (specs/005-backend-credential-preflight). The probe in
`adapters/openai_api/credential_probe.py` produces `BackendStatus` values; the
gate in `application/index_guard.py` caches and reports them; a running job
counts consecutive `BackendAuthError`s with `FailureStreak`.

Provider-neutral: the only signal is the HTTP status a backend answers with.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class CredentialState(str, Enum):
    VALID = "valid"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    #: Not required for this configuration (key-free embedder, summaries off).
    SKIPPED = "skipped"
    #: Required, but no check has run in this process yet.
    UNCHECKED = "unchecked"


ROLE_EMBEDDING = "embedding"
ROLE_SUMMARIZER = "summarizer"

# A 403 that is about the model, quota or region is not a rejected credential.
_NOT_A_CREDENTIAL_403 = ("model", "quota", "billing", "region", "country", "territor")


def classify(status_code: int | None, body_text: str = "") -> CredentialState:
    """Map a probe's HTTP status to a state. `None` means no response
    (timeout, DNS, TLS, connection refused)."""
    if status_code is None:
        return CredentialState.UNREACHABLE
    if 200 <= status_code < 300:
        return CredentialState.VALID
    if status_code == 401:
        return CredentialState.AUTH_FAILED
    if status_code == 403:
        lowered = (body_text or "").lower()
        if any(word in lowered for word in _NOT_A_CREDENTIAL_403):
            return CredentialState.UNREACHABLE
        return CredentialState.AUTH_FAILED
    return CredentialState.UNREACHABLE


_TOKEN_SHAPED = re.compile(r"sk-[A-Za-z0-9_\-*]{4,}")
_MIN_PREFIX = 4


def scrub(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """Remove every configured secret, any prefix of one (>= 4 chars), and any
    `sk-`-shaped token from `text`. Providers echo key prefixes in 401 bodies
    (the incident log did), so this runs on anything that reaches a log, a
    job error or a response."""
    out = text or ""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        for length in range(len(secret), _MIN_PREFIX - 1, -1):
            out = out.replace(secret[:length], "[redacted]")
    return _TOKEN_SHAPED.sub("[redacted]", out)


@dataclass
class BackendStatus:
    """The last known credential outcome for one endpoint+key."""

    roles: tuple[str, ...]
    state: CredentialState
    host: str = ""
    model: str = ""
    #: Setting names (never values) an operator edits to fix this backend.
    settings: tuple[str, ...] = ()
    status_code: int | None = None
    checked_at: float = 0.0  # wall clock, for display
    detail: str = field(default="", repr=False)


class BackendAuthError(RuntimeError):
    """A backend answered 401/403 for its credential. Carries no response
    body and no key: only the role, host, model and status."""

    def __init__(self, role: str, host: str, model: str, status: int):
        self.role = role
        self.host = host
        self.model = model
        self.status = status
        super().__init__(f"{role}_auth_failed: {role} backend {host} (model {model}) rejected its credential (HTTP {status})")


class FailureStreak:
    """Consecutive credential failures seen by one running job. A success
    resets the count; reaching the threshold marks the job aborted."""

    def __init__(self, threshold: int):
        self.threshold = max(1, int(threshold))
        self.consecutive = 0
        self.aborted = False
        self.last_error: BackendAuthError | None = None

    def record_failure(self, exc: BackendAuthError) -> None:
        self.consecutive += 1
        self.last_error = exc
        if self.consecutive >= self.threshold:
            self.aborted = True

    def record_success(self) -> None:
        self.consecutive = 0
