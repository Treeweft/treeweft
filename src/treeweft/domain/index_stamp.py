"""The index-stamp decision table (ADR-004 §3).

Pure logic: no imports from `treeweft.adapters`, `treeweft.application` or
`treeweft.infrastructure`, and no I/O or wall-clock reads. The adapters
observe each store (`StoreObservation`); `treeweft.application.index_guard`
orchestrates the check, holds the runtime cache, and does the actual store
reads/writes this module only decides about.

See `specs/001-index-schema-stamp/data-model.md` for the field-by-field
decision table this module implements.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Store = Literal["vector", "graph"]
StoreState = Literal["ok", "unverified", "reindex_required"]
IndexState = Literal["ok", "unverified", "reindex_required", "rebuilding"]
RebuildKind = Literal["none", "preparing", "rebuilding", "interrupted", "complete"]


@dataclass(frozen=True)
class IndexStamp:
    """What built a store's data (ADR-004 §3)."""

    schema: int
    embedding_model: str
    vector_dim: int


@dataclass(frozen=True)
class UnreadableStamp:
    """A stamp was present but could not be parsed.

    Never treated as "no stamp" — that would re-trigger adoption over
    corrupt data (data-model.md, IndexStamp "Round-trip").
    """

    detail: str


@dataclass(frozen=True)
class ConfiguredIndex:
    """What this process expects: INDEX_SCHEMA_VERSION, EMBEDDING_MODEL, VECTOR_DIM."""

    schema: int
    embedding_model: str
    vector_dim: int


@dataclass(frozen=True)
class StoreObservation:
    """What a store reports to the check. Adapters produce this; it holds no decisions."""

    store: Store
    backend: str
    exists: bool
    has_data: bool
    stamp: IndexStamp | UnreadableStamp | None
    schema_dim: int | None = None
    unreachable: str | None = None


@dataclass(frozen=True)
class Verification:
    """The legacy-adoption verification result (research R3).

    One of four states, built through the classmethods below:
    `passed()`, `failed(check, detail)`, `unavailable(detail)`, `not_run()`.
    """

    kind: Literal["passed", "failed", "unavailable", "not_run"]
    check: str | None = None
    detail: str | None = None

    @classmethod
    def passed(cls) -> "Verification":
        return cls("passed")

    @classmethod
    def failed(cls, check: str, detail: str) -> "Verification":
        return cls("failed", check, detail)

    @classmethod
    def unavailable(cls, detail: str) -> "Verification":
        return cls("unavailable", None, detail)

    @classmethod
    def not_run(cls) -> "Verification":
        return cls("not_run")


@dataclass(frozen=True)
class StoreCheck:
    """The decision for one store: its state, why (if not ok), and whether to stamp it."""

    store: Store
    backend: str
    state: StoreState
    reason: str | None = None
    write_stamp: bool = False


@dataclass(frozen=True)
class RebuildState:
    """The shared rebuild state, derived from Postgres (data-model.md "RebuildState")."""

    kind: RebuildKind
    done: int = 0
    total: int = 0


@dataclass(frozen=True)
class IndexStatus:
    """The per-process cached view, published by `refresh()` and read by /health."""

    state: IndexState
    reason: str | None = None
    rebuild_progress: tuple[int, int] | None = None
    preparing: bool = False


def format_reason(store: Store, backend: str, field: str, stored: object, configured: object) -> str:
    """FR-005's exact reason format for one mismatching field."""
    return f"{store} store ({backend}): {field} is {stored}, configured {configured}"


def parse_stamp(raw: dict | None) -> IndexStamp | UnreadableStamp | None:
    """Turn a store's raw stamp mapping into an `IndexStamp`, `UnreadableStamp`, or None.

    `raw` may hold string or native-typed values (Milvus/LanceDB/Chroma store
    strings; Neo4j/SQLite give native types). None or empty means "no stamp".
    """
    if not raw:
        return None
    missing = [k for k in ("index_schema", "embedding_model", "vector_dim") if k not in raw]
    if missing:
        return UnreadableStamp(f"missing key(s): {', '.join(missing)}")
    try:
        schema = int(raw["index_schema"])
        vector_dim = int(raw["vector_dim"])
    except (TypeError, ValueError) as exc:
        return UnreadableStamp(f"not an integer: {exc}")
    embedding_model = str(raw["embedding_model"])
    if not embedding_model:
        return UnreadableStamp("embedding_model is empty")
    return IndexStamp(schema=schema, embedding_model=embedding_model, vector_dim=vector_dim)


def _compare(stamp: IndexStamp, configured: ConfiguredIndex, store: Store, backend: str) -> list[str]:
    reasons = []
    if stamp.schema != configured.schema:
        reasons.append(format_reason(store, backend, "schema", stamp.schema, configured.schema))
    if stamp.embedding_model != configured.embedding_model:
        reasons.append(
            format_reason(store, backend, "embedding_model", stamp.embedding_model, configured.embedding_model)
        )
    if stamp.vector_dim != configured.vector_dim:
        reasons.append(format_reason(store, backend, "vector_dim", stamp.vector_dim, configured.vector_dim))
    return reasons


def decide_store(
    observation: StoreObservation,
    configured: ConfiguredIndex,
    verification: Verification,
    *,
    vector_has_data: bool | None = None,
) -> StoreCheck:
    """The ADR-004 §3 decision table for one store.

    `verification` is the legacy-adoption result for the vector store; for a
    graph observation it is the *same* result, reused per research R3 §3
    ("an unstamped graph store with data is adopted only when the vector
    store's verification passed in the same check"). `vector_has_data` is
    required when deciding a graph observation with data and no stamp.
    """
    store, backend = observation.store, observation.backend

    if observation.unreachable:
        return StoreCheck(
            store, backend, "unverified",
            reason=format_reason(store, backend, "unreachable", observation.unreachable, "reachable"),
        )

    if not observation.exists:
        # The vector collection hasn't been created yet. init_collection()
        # will stamp it when it does (research R2) — nothing to decide here.
        return StoreCheck(store, backend, "ok")

    if not observation.has_data and observation.stamp is None:
        # Fresh store: no data, no stamp. Stamp it now.
        return StoreCheck(store, backend, "ok", write_stamp=True)

    if observation.stamp is not None:
        if isinstance(observation.stamp, UnreadableStamp):
            return StoreCheck(
                store, backend, "reindex_required",
                reason=f"{store} store ({backend}): unreadable stamp: {observation.stamp.detail}",
            )
        mismatches = _compare(observation.stamp, configured, store, backend)
        if mismatches:
            return StoreCheck(store, backend, "reindex_required", reason="; ".join(mismatches))
        return StoreCheck(store, backend, "ok")

    # observation.stamp is None and observation.has_data is True: legacy
    # adoption territory (data but no stamp — every deployment before 1.0.0).
    if store == "graph" and not vector_has_data:
        return StoreCheck(
            store, backend, "reindex_required",
            reason=f"{store} store ({backend}): graph cannot be verified without vector data",
        )

    if verification.kind == "passed":
        return StoreCheck(store, backend, "ok", write_stamp=True)
    if verification.kind == "failed":
        return StoreCheck(
            store, backend, "reindex_required",
            reason=f"{store} store ({backend}): {verification.check}: {verification.detail}",
        )
    if verification.kind == "unavailable":
        return StoreCheck(
            store, backend, "unverified",
            reason=f"{store} store ({backend}): could not verify: {verification.detail}",
        )
    # not_run — never adopt without a verification result.
    return StoreCheck(
        store, backend, "unverified",
        reason=f"{store} store ({backend}): not yet verified",
    )


_STORE_PRECEDENCE: dict[StoreState, int] = {"reindex_required": 0, "unverified": 1, "ok": 2}


def aggregate(
    checks: Sequence[StoreCheck] = (), rebuild_state: RebuildState = RebuildState("none")
) -> IndexStatus:
    """Worst-of across the store checks and the rebuild state (data-model.md "Precedence").

    Order: preparing > interrupted > reindex_required > unverified > rebuilding > ok.
    """
    if rebuild_state.kind == "preparing":
        return IndexStatus("rebuilding", reason=None, rebuild_progress=(0, rebuild_state.total), preparing=True)

    if rebuild_state.kind == "interrupted":
        return IndexStatus("reindex_required", reason="a rebuild was interrupted; run it again")

    worst = min(checks, key=lambda c: _STORE_PRECEDENCE[c.state], default=None)
    if worst is not None and worst.state != "ok":
        if worst.state == "reindex_required":
            reasons = [c.reason for c in checks if c.state == "reindex_required" and c.reason]
            return IndexStatus("reindex_required", reason="; ".join(reasons))
        reasons = [c.reason for c in checks if c.state == "unverified" and c.reason]
        return IndexStatus("unverified", reason="; ".join(reasons) or None)

    if rebuild_state.kind == "rebuilding":
        return IndexStatus("rebuilding", reason=None, rebuild_progress=(rebuild_state.done, rebuild_state.total))

    return IndexStatus("ok")
