"""Shared mutable state for the indexer service.

Extracted from `indexer_service.py` so that module can be split without the
pieces importing each other. Everything here is process-wide state that more
than one part of the service reads or rebinds: the persistence adapters, the
in-memory job table, and the handles of the background loops.

**Access these through the module, never by importing the names.**

    from treeweft.application import indexer_state as state
    state._job_store          # correct — reads the current binding
    from .indexer_state import _job_store   # WRONG — snapshots it

The distinction is not stylistic. `_job_store`, `_job_queue` and the task
handles are all rebound at startup, and tests replace the stores. A module
that imported the name would hold whatever was bound at import time and
silently diverge from the rest of the service — passing its tests while
behaving differently in production, on the auth path.

This module imports nothing from `treeweft.application`, so it can never
participate in an import cycle.
"""

from __future__ import annotations

import os
from typing import Any

from treeweft.adapters.authorization.api_key_store import PostgreSQLApiKeyStore
from treeweft.adapters.authorization.group_store import PostgreSQLGroupStore
from treeweft.adapters.authorization.grant_store import PostgreSQLGrantStore
from treeweft.adapters.authorization.search_audit_store import (
    PostgreSQLSearchAuditStore,
)
from treeweft.adapters.authorization.session_store import PostgreSQLSessionStore
from treeweft.adapters.authorization.token_store import (
    PostgreSQLPersonalAccessTokenStore,
)
from treeweft.adapters.authorization.user_store import PostgreSQLUserStore
from treeweft.adapters.postgresql.job_file_error_store import JobFileErrorStore
from treeweft.adapters.postgresql.login_attempt_store import LoginAttemptStore
from treeweft.adapters.sources.repository import PostgreSourceRepository

# ── Persistence adapters ─────────────────────────────────────────────────
_source_repo = PostgreSourceRepository()
_user_store = PostgreSQLUserStore()
_group_store = PostgreSQLGroupStore()
_grant_store = PostgreSQLGrantStore()
_search_audit = PostgreSQLSearchAuditStore()
_token_store = PostgreSQLPersonalAccessTokenStore()
_api_key_store = PostgreSQLApiKeyStore()
_session_store = PostgreSQLSessionStore()

# Per-file error log. Lazy-initialized — the Postgres pool may not be up when
# the first read happens, and the write paths swallow pool-absent.
_job_file_error_store = JobFileErrorStore()

# Login rate-limit sliding window. Fail-loud on no pool.
_login_attempt_store = LoginAttemptStore()

# ── Job state ────────────────────────────────────────────────────────────
# The in-memory job table. Rebound never, mutated constantly — which is why
# `patch.dict(state._jobs, ...)` works across modules where `patch.object`
# on a name would not.
_jobs: dict[str, dict[str, Any]] = {}
_last_job_id: str | None = None

# Initialized on startup once the pool exists.
_job_store = None          # JobStore | None
_job_group_store = None    # JobGroupStore | None
_job_queue = None          # PostgresJobQueue | None

# ── Shared configuration ─────────────────────────────────────────────────
# Read by more than one of the split modules, so it cannot live in any one of
# them: a module-level copy elsewhere is a snapshot, and patching it in tests
# (or overriding it at runtime) reaches one reader and not the other.
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "500"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ── Background loop handles ──────────────────────────────────────────────
# Status record polled via GET /build-community. Only one build runs at a time.
_community_build_state: dict = {"status": "idle"}
_community_build_task: "Any | None" = None
_freshness_sampler_task: "Any | None" = None
_login_purge_task: "Any | None" = None
_fleet_refresh_task: "Any | None" = None
