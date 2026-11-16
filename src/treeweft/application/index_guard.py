"""The index-schema stamp check, gate and (later) rebuild (ADR-004 §3).

Orchestrates `domain/index_stamp.py`'s pure decision table against the real
stores (through the `treeweft.retriever` / `treeweft.graph_store` shims) and
Postgres (through `adapters/postgresql`), and holds this process's cached
view of the result. See `specs/001-index-schema-stamp/research.md` R2-R5,
R13 for the design this module implements.

Every process caches its own view (`status()`, read by `/health` and the
route gates — never touches a store or Postgres). The authoritative,
cross-process check for a job about to write is `dispatch_allowed()`, which
always reads live state.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from datetime import datetime, timezone

from starlette.responses import JSONResponse

from treeweft import embedder, graph_store, retriever, versions
from treeweft.adapters.postgresql import maintenance_lock
from treeweft.domain.index_stamp import (
    ConfiguredIndex,
    IndexStamp,
    IndexStatus,
    RebuildState,
    StoreCheck,
    StoreObservation,
    Verification,
    aggregate,
    decide_store,
)
from treeweft.domain.credentials import (
    ROLE_EMBEDDING,
    ROLE_SUMMARIZER,
    BackendStatus,
    CredentialState,
)
from treeweft.domain.jobs import JobStatus
from treeweft.infrastructure.config import (
    credential_check_seconds,
    credential_retry_seconds,
    index_status_refresh_seconds,
    index_verify_interval_seconds,
    index_verify_timeout_seconds,
)

logger = logging.getLogger(__name__)

_MAX_DETAIL = 300
_LEGACY_SAMPLE_SIZE = 3
_LEGACY_COSINE_THRESHOLD = 0.99
_LEGACY_SCAN_LIMIT = 20
_LEGACY_SCAN_LIMIT_FALLBACK = 200

# ── Cached state ─────────────────────────────────────────────────────────
_status = IndexStatus("reindex_required", reason="index status not yet checked")
_refreshed_at: datetime | None = None
_last_rebuild_kind = "none"
_lock = asyncio.Lock()
_refresh_task: "asyncio.Task | None" = None
_last_verify_attempt = 0.0

# job_id -> refusal detail, set by dispatch_allowed() and consumed once by
# refusal_detail(). Keyed per job so concurrent workers never race on a
# shared "last" value.
_refusal_details: dict[str, str] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def configured() -> ConfiguredIndex:
    """What this process expects. Read fresh every call (never cached at
    import) so tests can monkeypatch the environment."""
    return ConfiguredIndex(
        schema=versions.INDEX_SCHEMA_VERSION,
        embedding_model=os.environ["EMBEDDING_MODEL"],
        vector_dim=int(os.environ["VECTOR_DIM"]),
    )


# ── Legacy verification (research R3) ───────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def _verify_vector_store(observation: StoreObservation, cfg: ConfiguredIndex) -> Verification:
    """Legacy adoption check (research R3): dimension, then up to 3 sampled
    chunks re-embedded and compared by cosine similarity ≥ 0.99. Never
    raises — an unreachable embedding service or a timeout is reported as
    `unavailable`, not propagated.
    """
    if observation.schema_dim is not None and observation.schema_dim != cfg.vector_dim:
        return Verification.failed(
            "dimension", f"stored dimension is {observation.schema_dim}, configured {cfg.vector_dim}"
        )

    try:
        samples = await retriever.sample_chunks(_LEGACY_SAMPLE_SIZE, scan_limit=_LEGACY_SCAN_LIMIT)
        if not samples:
            samples = await retriever.sample_chunks(_LEGACY_SAMPLE_SIZE, scan_limit=_LEGACY_SCAN_LIMIT_FALLBACK)
    except Exception as exc:  # noqa: BLE001
        logger.warning("legacy verification: sample_chunks failed: %s", exc)
        return Verification.unavailable(str(exc))

    if not samples:
        return Verification.failed("no verifiable chunks", "no chunk under 50000 characters among inspected rows")

    texts = [text for text, _ in samples]
    try:
        embeddings = await asyncio.wait_for(embedder.embed(texts), timeout=index_verify_timeout_seconds())
    except asyncio.TimeoutError:
        return Verification.unavailable("embedding timed out")
    except Exception as exc:  # noqa: BLE001
        logger.warning("legacy verification: embed() failed: %s", exc)
        return Verification.unavailable(str(exc))

    cosines = [_cosine(embeddings[i], samples[i][1]) for i in range(len(samples))]
    logger.info("legacy verification: sampled=%d cosines=%s", len(samples), [round(c, 4) for c in cosines])

    worst = min(cosines) if cosines else 0.0
    if worst < _LEGACY_COSINE_THRESHOLD:
        return Verification.failed(
            "embedding_model", f"cosine {worst:.4f} < {_LEGACY_COSINE_THRESHOLD} on a sampled chunk"
        )
    return Verification.passed()


# ── Rebuild state (research R7, R13) ────────────────────────────────────

async def _rebuild_state_and_group() -> "tuple[RebuildState, str | None]":
    """The live rebuild state and the id of the latest `index-rebuild`
    group, read directly from Postgres. Shared by `refresh()` (cached) and
    `dispatch_allowed()` (always fresh)."""
    from treeweft.application import indexer_state as idx_state

    if idx_state._job_group_store is None or idx_state._job_store is None:
        return RebuildState("none"), None

    lock_mode = await maintenance_lock.probe()
    group = await idx_state._job_group_store.latest_by_kind("index-rebuild")
    group_id = group.id if group else None

    if lock_mode == "exclusive":
        return RebuildState("preparing", total=(group.task_count if group else 0)), group_id
    if group is None:
        return RebuildState("none"), None

    total = group.task_count
    if total == 0:
        return RebuildState("complete"), group_id

    jobs = await idx_state._job_store.list_by_group(group.id)
    active = sum(1 for j in jobs if j.status in (JobStatus.QUEUED, JobStatus.RUNNING))
    if active > 0:
        terminal = sum(1 for j in jobs if j.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.DEAD_LETTER))
        return RebuildState("rebuilding", done=terminal, total=total), group_id
    if len(jobs) < total:
        return RebuildState("interrupted"), group_id
    return RebuildState("complete"), group_id


async def _rebuild_state_now() -> RebuildState:
    state, _ = await _rebuild_state_and_group()
    return state


# ── The check itself ─────────────────────────────────────────────────────

async def _write_stamp_if_needed(check: StoreCheck, write_fn, cfg: ConfiguredIndex) -> None:
    if not check.write_stamp:
        return
    handle = await maintenance_lock.acquire("shared")
    if handle is None:
        # Another process holds the lock exclusively (a rebuild is
        # preparing) — never stamp over it (research R13). The overall
        # status still reports `rebuilding` via the rebuild-state check.
        return
    try:
        stamp = IndexStamp(schema=cfg.schema, embedding_model=cfg.embedding_model, vector_dim=cfg.vector_dim)
        await write_fn(stamp)
    finally:
        await handle.release()


async def _compute_status(*, verify: bool) -> IndexStatus:
    """Observe both stores, decide, optionally stamp, and aggregate with
    the live rebuild state. `verify=False` skips legacy (embedding-based)
    verification — research R5 §2 rule 4's cheap re-observation."""
    cfg = configured()
    vector_obs = await retriever.observe_index()
    graph_obs = await graph_store.observe_index()

    needs_verification = (
        verify
        and not vector_obs.unreachable
        and vector_obs.exists
        and vector_obs.has_data
        and vector_obs.stamp is None
    )
    verification = await _verify_vector_store(vector_obs, cfg) if needs_verification else Verification.not_run()

    vector_check = decide_store(vector_obs, cfg, verification)
    graph_check = decide_store(graph_obs, cfg, verification, vector_has_data=vector_obs.has_data)

    await _write_stamp_if_needed(vector_check, retriever.write_stamp, cfg)
    await _write_stamp_if_needed(graph_check, graph_store.write_stamp, cfg)

    rebuild_state = await _rebuild_state_now()
    global _last_rebuild_kind
    if rebuild_state.kind != _last_rebuild_kind:
        # A rebuild started, finished or was elsewhere completed since the
        # last check: this process's cached community embeddings may be
        # for a graph that no longer exists (ADR-004 §3 / research R7).
        from treeweft.application import retrieval

        retrieval.invalidate_graph_caches()
        _last_rebuild_kind = rebuild_state.kind

    return aggregate([vector_check, graph_check], rebuild_state)


async def run_check() -> IndexStatus:
    """The full check, with legacy verification. Called at startup and
    inline by `require_writable()` while `unverified`."""
    global _status, _refreshed_at, _last_verify_attempt
    async with _lock:
        _status = await _compute_status(verify=True)
        _refreshed_at = _now()
        _last_verify_attempt = asyncio.get_event_loop().time()
        if _status.reason:
            logger.info("index_status: %s (%s)", _status.state, _status.reason)
        else:
            logger.info("index_status: %s", _status.state)
        return _status


async def refresh() -> IndexStatus:
    """The periodic refresh (research R4): always re-reads the rebuild
    state (cheap); re-observes the stamps only while not `ok`.

    `unverified` and `reindex_required` are both re-checked with full
    (embedding) verification once the retry interval has elapsed — never
    with the cheap, no-embedding path. A store whose current state came
    from a legacy-verification failure (no stamp, has data) cannot be
    re-derived cheaply: `decide_store` reports `unverified` for a
    not-yet-run check, so a cheap re-observation would silently soften a
    real, already-confirmed failure into "not yet verified" on the very
    next tick, before the retry interval ever elapses. A stamp-mismatch
    `reindex_required` costs nothing extra here either way: decide_store
    never calls the embedder once a stamp is present, so this is not
    wasted verification for that case (bug found live in T058: the pre-fix
    version misreported a confirmed Milvus "no verifiable chunks" failure
    as merely `unverified` within one refresh tick)."""
    global _status, _refreshed_at, _last_verify_attempt
    async with _lock:
        rebuild_state = await _rebuild_state_now()
        if _status.state == "ok" and rebuild_state.kind in ("none", "complete"):
            return _status

        do_verify = False
        if _status.state in ("unverified", "reindex_required"):
            now = asyncio.get_event_loop().time()
            if now - _last_verify_attempt >= index_verify_interval_seconds():
                do_verify = True

        _status = await _compute_status(verify=do_verify)
        _refreshed_at = _now()
        if do_verify:
            _last_verify_attempt = asyncio.get_event_loop().time()
        return _status


async def _refresh_loop() -> None:
    while True:
        await asyncio.sleep(index_status_refresh_seconds())
        try:
            await refresh()
        except Exception:  # noqa: BLE001 — the loop must never die
            logger.exception("index_guard refresh failed")


def start_refresh_loop() -> None:
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(_refresh_loop())


async def stop_refresh_loop() -> None:
    global _refresh_task
    if _refresh_task is not None:
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass
        _refresh_task = None


# ── Cached-view accessors ────────────────────────────────────────────────

def status() -> IndexStatus:
    return _status


def writes_allowed() -> bool:
    s = _status
    if s.state == "ok":
        return True
    if s.state == "rebuilding" and not s.preparing:
        return True
    return False


def health_fields() -> dict:
    s = _status
    out: dict = {"index_schema": versions.INDEX_SCHEMA_VERSION, "index_status": s.state}
    if s.state in ("reindex_required", "unverified") and s.reason:
        out["reindex_reason"] = s.reason
    if s.state == "rebuilding" and s.rebuild_progress is not None:
        out["rebuild_progress"] = {"done": s.rebuild_progress[0], "total": s.rebuild_progress[1]}
    out["credentials"] = credential_health()
    return out


# ── 409 bodies (contracts/errors.md) ─────────────────────────────────────

def _truncate_for_detail(prefix: str, reason: str, suffix: str) -> str:
    """Fit prefix + reason + suffix into `_MAX_DETAIL` chars, truncating
    only the reason, so the rebuild pointer in `suffix` always survives."""
    budget = _MAX_DETAIL - len(prefix) - len(suffix)
    if budget < 0:
        budget = 0
    if len(reason) > budget:
        reason = reason[: max(budget - 1, 0)].rstrip() + "…"
    return f"{prefix}{reason}{suffix}"


def _reindex_required_body(reason: str) -> dict:
    prefix = "Index requires rebuild: "
    suffix = ". Run POST /index/rebuild?dry_run=true, then POST /index/rebuild (docs/upgrading.md)."
    return {
        "detail": _truncate_for_detail(prefix, reason, suffix),
        "reason": reason,
        "index_status": "reindex_required",
        "rebuild": "/index/rebuild",
    }


def _unverified_body(reason: str) -> dict:
    prefix = "Index unverified: "
    suffix = ". Index jobs are refused until the embedding model is verified; this retries automatically."
    return {
        "detail": _truncate_for_detail(prefix, reason, suffix),
        "reason": reason,
        "index_status": "unverified",
        "rebuild": "/index/rebuild",
    }


def _preparing_body() -> dict:
    return {
        "detail": (
            "Index rebuild in progress: stores are being recreated. Retry shortly; "
            "progress is in GET /health."
        ),
        "index_status": "rebuilding",
        "rebuild": "/index/rebuild",
    }


def _parsers_unavailable_body(failures: dict[str, str]) -> dict:
    prefix = "Tree-sitter parsers unavailable for: "
    suffix = ". Index jobs are refused until they load; this retries automatically."
    return {
        "detail": _truncate_for_detail(prefix, ", ".join(sorted(failures)), suffix),
        "reason": "parsers_unavailable",
        "index_status": "parsers_unavailable",
        "languages": dict(failures),
    }


# ── Route gates ───────────────────────────────────────────────────────────

def require_searchable() -> "JSONResponse | None":
    """The gate for the six read routes. Returns a 409 `JSONResponse` to
    return immediately, or None to proceed (errors.md)."""
    s = _status
    if s.preparing:
        return JSONResponse(status_code=409, content=_preparing_body())
    if s.state == "reindex_required":
        return JSONResponse(status_code=409, content=_reindex_required_body(s.reason or "index mismatch"))
    return None


async def require_writable() -> "JSONResponse | None":
    """The gate for routes that enqueue an index job or run the community
    build. While `unverified`, runs one inline re-check first (FR-011)."""
    s = _status
    if s.preparing:
        return JSONResponse(status_code=409, content=_preparing_body())
    if s.state == "unverified":
        s = await run_check()
        if s.preparing:
            return JSONResponse(status_code=409, content=_preparing_body())
        if s.state == "unverified":
            return JSONResponse(status_code=409, content=_unverified_body(s.reason or "not yet verified"))
    if s.state == "reindex_required":
        return JSONResponse(status_code=409, content=_reindex_required_body(s.reason or "index mismatch"))
    return None


async def store_error_response(exc: BaseException) -> "JSONResponse | None":
    """Called from a read route's except clause. When a store call fails
    because a rebuild is currently dropping/recreating it, returns the
    `preparing` 409 to raise instead; otherwise None (re-raise `exc`)."""
    mode = await maintenance_lock.probe()
    if mode == "exclusive":
        logger.info("store call failed while a rebuild is preparing; reporting 409 instead of %r", exc)
        return JSONResponse(status_code=409, content=_preparing_body())
    return None


# ── Dispatch gate (authoritative, cross-process — research R5 §2) ───────

async def _recheck_stamps_cheap() -> IndexStatus:
    """Re-observe the stamps with no embedding (research R5 §2 rule 4),
    publishing the result."""
    global _status, _refreshed_at
    async with _lock:
        _status = await _compute_status(verify=False)
        _refreshed_at = _now()
        return _status


async def dispatch_allowed(job) -> bool:
    """The authoritative, cross-process check a queue worker runs just
    before actually dispatching a popped job (research R5 §2). Always
    reads live state — never the cache alone."""
    rebuild_state, latest_group_id = await _rebuild_state_and_group()

    is_rebuild_job = latest_group_id is not None and job.group_id == latest_group_id
    if is_rebuild_job and rebuild_state.kind != "interrupted":
        _refusal_details.pop(job.id, None)
        return True

    if rebuild_state.kind == "preparing":
        _refusal_details[job.id] = _preparing_body()["detail"]
        return False
    if rebuild_state.kind == "interrupted":
        _refusal_details[job.id] = _reindex_required_body("a rebuild was interrupted; run it again")["detail"]
        return False

    if _status.state in ("reindex_required", "unverified"):
        fresh = await _recheck_stamps_cheap()
        if fresh.state == "reindex_required":
            _refusal_details[job.id] = _reindex_required_body(fresh.reason or "index mismatch")["detail"]
            return False
        if fresh.state == "unverified":
            _refusal_details[job.id] = _unverified_body(fresh.reason or "not yet verified")["detail"]
            return False

    _refusal_details.pop(job.id, None)
    return True


async def require_parsers() -> "JSONResponse | None":
    """409 when a `LANGUAGE_MAP` tree-sitter parser cannot load, so an index
    job is refused instead of silently chunking on the TokenChunker fallback.
    Call right after `require_writable()` on every route that parses code."""
    from treeweft.adapters.tree_sitter import parsers

    readiness = await parsers.check_parsers_async()
    if readiness.ok:
        return None
    return JSONResponse(status_code=409, content=_parsers_unavailable_body(readiness.failures))


def refusal_detail(job_id: str) -> str:
    """The refusal text for a job `dispatch_allowed()` just refused.
    Consumes the entry (each job's detail is read at most once)."""
    return _refusal_details.pop(job_id, "Index requires rebuild. See GET /health.")


# ── Backend credentials (specs/005-backend-credential-preflight) ──────────
#
# A bad embeddings key used to be discovered one file at a time: every file
# failed fast and the job stayed `running`. `require_credentials()` refuses the
# index routes instead, with one cached, single-flight probe per distinct
# endpoint+key. Only an authentication failure refuses; an unreachable
# backend never does. The cache key (a hash of the key) lives in memory only
# and is never logged or returned.

@dataclass(frozen=True)
class _Target:
    role: str
    kind: str  # "embeddings" | "chat"
    url: str
    model: str
    key: str
    setting: str

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc

    @property
    def cache_key(self) -> tuple[str, str]:
        return (self.host, hashlib.sha256(self.key.encode()).hexdigest())


_cred_cache: dict[tuple[str, str], tuple[BackendStatus, float]] = {}
_cred_locks: dict[tuple[str, str], asyncio.Lock] = {}
_cred_view: dict[str, BackendStatus] = {}
_cred_logged: dict[str, CredentialState] = {}
_cred_clock = time.monotonic


def reset_credential_cache() -> None:
    _cred_cache.clear()
    _cred_locks.clear()
    _cred_view.clear()
    _cred_logged.clear()


def _key_and_setting(*names: str) -> tuple[str, str]:
    for name in names:
        value = os.environ.get(name, "")
        if value:
            return value, name
    return "", names[0]


def _embedding_target() -> "_Target | None":
    """None when no credential applies: the TEI path takes no key, and an
    OpenAI-compatible endpoint with no key set is key-free (FR-011)."""
    if os.environ.get("EMBEDDING_PROVIDER", "tei") != "openai":
        return None
    key, setting = _key_and_setting("EMBEDDING_API_KEY", "OPENAI_API_KEY")
    if not key:
        return None
    return _Target(
        ROLE_EMBEDDING, "embeddings",
        os.environ.get("EMBEDDING_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
        key, setting,
    )


def _summarizer_target() -> "_Target | None":
    """Only when summaries are enabled for indexing (FR-006): an LLM used only
    at search time (HyDE) is outside the gate."""
    if os.environ.get("USE_SUMMARY_VECTOR", "1") != "1":
        return None
    url = os.environ.get("LLM_URL", "")
    key, setting = _key_and_setting("LLM_API_KEY", "OPENAI_API_KEY")
    if not url or not key:
        return None
    return _Target(
        ROLE_SUMMARIZER, "chat", url.rstrip("/"), os.environ.get("LLM_MODEL", ""), key, setting
    )


def _targets() -> list[_Target]:
    return [t for t in (_embedding_target(), _summarizer_target()) if t is not None]


def _group_targets(targets: list[_Target]) -> list[list[_Target]]:
    """Targets that share endpoint host and key are one probe (FR-006a)."""
    groups: dict[tuple[str, str], list[_Target]] = {}
    for t in targets:
        groups.setdefault(t.cache_key, []).append(t)
    return list(groups.values())


def _log_credential_transition(status: BackendStatus) -> None:
    for role in status.roles:
        if _cred_logged.get(role) is status.state:
            continue
        prior = _cred_logged.get(role)
        _cred_logged[role] = status.state
        if status.state is CredentialState.AUTH_FAILED:
            logger.warning(
                "%s backend %s rejected its credential (HTTP %s, model %s): index jobs are "
                "refused until %s is corrected",
                role, status.host, status.status_code, status.model, " or ".join(status.settings),
            )
        elif status.state is CredentialState.UNREACHABLE:
            logger.warning(
                "%s backend %s could not be reached to check its credential; not blocking",
                role, status.host,
            )
        elif prior in (CredentialState.AUTH_FAILED, CredentialState.UNREACHABLE):
            logger.info("%s backend %s credential check passed again", role, status.host)


async def _probe_group(group: list[_Target]) -> BackendStatus:
    from treeweft.adapters.openai_api import credential_probe

    first = group[0]
    probe = credential_probe.probe_embeddings if first.kind == "embeddings" else credential_probe.probe_chat
    state, code = await probe(first.url, first.model, first.key)
    settings = tuple(dict.fromkeys(t.setting for t in group))
    return BackendStatus(
        roles=tuple(t.role for t in group), state=state, host=first.host, model=first.model,
        settings=settings, status_code=code, checked_at=time.time(),
    )


async def check_backends(force: bool = False) -> list[BackendStatus]:
    """Probe each distinct required backend (cached; concurrent callers share
    one probe). Returns one status per probed endpoint+key. Never raises."""
    results: list[BackendStatus] = []
    targets = _targets()
    for role in (ROLE_EMBEDDING, ROLE_SUMMARIZER):
        if not any(t.role == role for t in targets):
            _cred_view[role] = BackendStatus(roles=(role,), state=CredentialState.SKIPPED)
    for group in _group_targets(targets):
        key = group[0].cache_key
        lock = _cred_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = _cred_cache.get(key)
            ttl = None
            if cached is not None and not force:
                ttl = (
                    credential_check_seconds()
                    if cached[0].state is CredentialState.VALID
                    else credential_retry_seconds()
                )
            if cached is not None and ttl is not None and _cred_clock() - cached[1] < ttl:
                status = cached[0]
            else:
                try:
                    status = await _probe_group(group)
                except Exception:  # noqa: BLE001 - a probe bug must not block indexing
                    logger.exception("credential probe failed unexpectedly; not blocking")
                    status = BackendStatus(
                        roles=tuple(t.role for t in group), state=CredentialState.UNREACHABLE,
                        host=group[0].host, model=group[0].model,
                    )
                _cred_cache[key] = (status, _cred_clock())
                _log_credential_transition(status)
        for role in status.roles:
            _cred_view[role] = status
        results.append(status)
    return results


def _credential_reason(failed: list[BackendStatus]) -> str:
    roles = {r for s in failed for r in s.roles}
    return "embedding_auth_failed" if ROLE_EMBEDDING in roles else "summarizer_auth_failed"


def _credential_refusal_body(failed: list[BackendStatus]) -> dict:
    first = failed[0]
    label = " and ".join(first.roles)
    detail = (
        f"The {label} backend rejected its credential (HTTP {first.status_code}): "
        f"{first.host}, model {first.model}. Check {' / '.join(first.settings)}."
    )
    if len(failed) > 1:
        detail += f" {len(failed) - 1} more backend(s) also failed; see backends."
    return {
        "detail": detail[:_MAX_DETAIL],
        "reason": _credential_reason(failed),
        "index_status": "credentials_invalid",
        "backends": [
            {
                "roles": list(s.roles), "host": s.host, "model": s.model,
                "state": s.state.value, "settings": list(s.settings),
            }
            for s in failed
        ],
    }


async def require_credentials() -> "JSONResponse | None":
    """409 when a required backend rejected its credential, so an index job is
    refused up front. Call right after `require_parsers()`. An unreachable
    backend never refuses (it is not evidence of a bad key)."""
    failed = [s for s in await check_backends() if s.state is CredentialState.AUTH_FAILED]
    if not failed:
        return None
    return JSONResponse(status_code=409, content=_credential_refusal_body(failed))


def credential_health() -> dict:
    """The public `/health` view: role, state and check time only, from the
    cached results (never probes, never host/model/reason: Principle VI)."""
    required = {t.role for t in _targets()}
    backends = []
    for role in (ROLE_EMBEDDING, ROLE_SUMMARIZER):
        if role not in required:
            backends.append({"role": role, "state": CredentialState.SKIPPED.value, "checked_at": None})
            continue
        status = _cred_view.get(role)
        if status is None or status.state is CredentialState.SKIPPED:
            backends.append({"role": role, "state": CredentialState.UNCHECKED.value, "checked_at": None})
            continue
        backends.append({
            "role": role,
            "state": status.state.value,
            "checked_at": datetime.fromtimestamp(status.checked_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    ready = not any(b["state"] == CredentialState.AUTH_FAILED.value for b in backends)
    return {"ready_for_indexing": ready, "backends": backends}


# ── Rebuild (research R7) ─────────────────────────────────────────────────

async def _job_blockers() -> list[str]:
    from treeweft.application import indexer_state as idx_state

    blockers: list[str] = []
    if idx_state._job_store is not None:
        for job_status in (JobStatus.QUEUED, JobStatus.RUNNING):
            jobs = await idx_state._job_store.list_by_status(job_status)
            if jobs:
                blockers.append(f"{len(jobs)} job(s) {job_status.value}")
    return blockers


async def _pre_rebuild_blockers() -> list[str]:
    """Step 0's blockers: job activity plus the maintenance lock in any
    mode. Never call this after taking the lock ourselves — use
    `_job_blockers()` for the step-3 re-check instead."""
    blockers = await _job_blockers()
    lock_mode = await maintenance_lock.probe()
    if lock_mode is not None:
        blockers.append(f"the maintenance lock is held ({lock_mode})")
    return blockers


class RebuildRefused(Exception):
    """The real rebuild call was refused before anything was written."""

    def __init__(self, detail: str, blockers: list[str]):
        super().__init__(detail)
        self.detail = detail
        self.blockers = blockers


class RebuildFailed(Exception):
    """A step of the real rebuild raised after stores may have changed."""

    def __init__(self, step: str, detail: str):
        super().__init__(detail)
        self.step = step
        self.detail = detail


async def rebuild(dry_run: bool, user_id: str | None = None) -> dict:
    """`POST /index/rebuild` (ADR-004 §3, research R7). Returns the
    response body for a dry run or a successful real call. Raises
    `RebuildRefused` (409, nothing written), `RebuildFailed` (503, a step
    failed) or `RuntimeError` (503, no Postgres)."""
    from treeweft.application import indexer_runners as runners
    from treeweft.application import indexer_service as idx_svc
    from treeweft.application import indexer_state as idx_state
    from treeweft.application import retrieval

    if idx_state._job_group_store is None or idx_state._job_store is None or idx_state._job_queue is None:
        raise RuntimeError("rebuild requires the job database")

    sources = await idx_svc._all_source_dicts()
    total_chunks = sum(int(s.get("chunk_count") or 0) for s in sources)
    logger.warning(
        "event=index_rebuild dry_run=%s user=%s sources=%d total_chunks=%d",
        dry_run, user_id, len(sources), total_chunks,
    )
    source_summaries = [
        {
            "id": s["id"],
            "label": s.get("url") or s.get("path") or s["id"],
            "kind": s.get("kind") or "repo",
            "chunk_count": int(s.get("chunk_count") or 0),
        }
        for s in sources
    ]

    if dry_run:
        return {
            "dry_run": True,
            "sources": source_summaries,
            "total_sources": len(sources),
            "total_chunks": total_chunks,
            "index_status": status().state,
            "blockers": await _pre_rebuild_blockers(),
        }

    # Step 0
    blockers = await _pre_rebuild_blockers()
    if blockers:
        raise RebuildRefused(f"Rebuild refused: {'; '.join(blockers)}", blockers)

    # Step 1
    handle = await maintenance_lock.acquire("exclusive")
    if handle is None:
        raise RebuildRefused("another rebuild or community build is in progress", [])

    # Step 2
    group_id = await idx_state._job_group_store.create(
        label="index rebuild", kind="index-rebuild", created_by=user_id, task_count=len(sources),
    )
    logger.warning("event=index_rebuild stage=group_created group_id=%s sources=%d", group_id, len(sources))

    # Step 3
    blockers = await _job_blockers()
    if blockers:
        await idx_state._job_group_store.delete(group_id)
        await handle.release()
        raise RebuildRefused(f"Rebuild refused: {'; '.join(blockers)}", blockers)

    # Step 4
    try:
        await retriever.drop_index()
        await retriever.init_collection()  # stamps it (research R2)
        await graph_store.clear_index_data()
        cfg = configured()
        await graph_store.write_stamp(
            IndexStamp(schema=cfg.schema, embedding_model=cfg.embedding_model, vector_dim=cfg.vector_dim)
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("event=index_rebuild stage=recreate_stores_failed group_id=%s", group_id)
        global _status, _refreshed_at
        async with _lock:
            _status = IndexStatus("reindex_required", reason=f"rebuild failed at recreate stores: {exc}")
            _refreshed_at = _now()
        await handle.release()
        raise RebuildFailed("recreate stores", str(exc)) from exc

    retrieval.invalidate_graph_caches()
    logger.warning("event=index_rebuild stage=stores_recreated group_id=%s", group_id)

    # Step 5
    for src in sources:
        job = idx_svc._build_source_reindex_job(src, group_id=group_id)
        await runners._persist_job(job)
        await idx_state._job_queue.enqueue(job["job_id"])
    logger.warning("event=index_rebuild stage=jobs_enqueued group_id=%s jobs=%d", group_id, len(sources))

    # Step 6
    await handle.release()
    await refresh()

    return {
        "dry_run": False,
        "group_id": group_id,
        "total_sources": len(sources),
        "total_chunks_before": total_chunks,
        "index_status": status().state,
    }
