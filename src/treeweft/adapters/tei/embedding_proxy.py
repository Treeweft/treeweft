"""Cost-aware embedding proxy for multiple TEI backends.

Routes each batch to the backend with the lowest *in-flight estimated token
cost* rather than the lowest connection count. Token cost is estimated as
`sum(len(t) // 3 for t in batch)` — the same heuristic `_pick_url` already
uses for GPU/CPU classification.

Preserves the predictive GPU/CPU split: any batch containing a chunk whose
estimated tokens exceed `GPU_MAX_TOKENS` is pinned to a CPU-class backend.
Falls back to the next-cheapest backend on HTTP error.

Interface is compatible with the existing `embed(texts) -> list[list[float]]`
free function — `EmbeddingProxy.embed` is the same shape.

See docs/adr-001-cost-aware-embedding-proxy.md for the rationale.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class _CostEstimator(Protocol):
    def batch_cost(self, texts: list[str]) -> int: ...
    def max_chunk_cost(self, texts: list[str]) -> int: ...


class _CharHeuristicEstimator:
    """Char/3 fallback when the real tokenizer cannot be loaded."""

    def batch_cost(self, texts: list[str]) -> int:
        return sum(len(t) // 3 for t in texts)

    def max_chunk_cost(self, texts: list[str]) -> int:
        return max((len(t) // 3 for t in texts), default=0)


class _TokenizerEstimator:
    """Real tokenizer from HuggingFace, keyed by EMBEDDING_MODEL."""

    def __init__(self, model: str) -> None:
        from tokenizers import Tokenizer

        try:
            self._tok = Tokenizer.from_pretrained(model)
        except Exception:
            from huggingface_hub import hf_hub_download

            path = hf_hub_download(repo_id=model, filename="tokenizer.json")
            self._tok = Tokenizer.from_file(path)

    def _count(self, text: str) -> int:
        return len(self._tok.encode(text, add_special_tokens=False).ids)

    def batch_cost(self, texts: list[str]) -> int:
        encs = self._tok.encode_batch(texts, add_special_tokens=False)
        return sum(len(e.ids) for e in encs)

    def max_chunk_cost(self, texts: list[str]) -> int:
        if not texts:
            return 0
        encs = self._tok.encode_batch(texts, add_special_tokens=False)
        return max(len(e.ids) for e in encs)


_estimator: _CostEstimator | None = None


def _get_estimator() -> _CostEstimator:
    global _estimator
    if _estimator is not None:
        return _estimator
    model = os.environ.get("EMBEDDING_MODEL", "").strip()
    if not model:
        logger.warning("[embed] EMBEDDING_MODEL not set; using char/3 cost heuristic")
        _estimator = _CharHeuristicEstimator()
        return _estimator
    try:
        _estimator = _TokenizerEstimator(model)
        logger.info("[embed] loaded tokenizer for %s", model)
    except Exception as exc:
        logger.warning("[embed] failed to load tokenizer for %s (%s); using char/3 heuristic", model, exc)
        _estimator = _CharHeuristicEstimator()
    return _estimator


class BackendClass(Enum):
    GPU = "gpu"
    CPU = "cpu"


class CircuitState(Enum):
    CLOSED = "closed"        # Normal operation — requests pass through
    OPEN = "open"            # Tripped — requests blocked until cooldown
    HALF_OPEN = "half_open"  # Probing — single request allowed through


# Circuit breaker defaults (overridable via env)
_CB_FAILURE_THRESHOLD = int(os.environ.get("EMBED_CB_THRESHOLD", "5"))
_CB_WINDOW_SECONDS = float(os.environ.get("EMBED_CB_WINDOW", "60.0"))
_CB_COOLDOWN_SECONDS = float(os.environ.get("EMBED_CB_COOLDOWN", "30.0"))

# Adaptive per-backend concurrency (back-off under load). A backend answers
# at most `concurrency_limit` /embed requests at once; the rest wait in the
# proxy, where the HTTP timeout does not run. The limit halves on a read
# timeout, a 429, or an answer slower than half the read timeout, and grows
# while answers are fast and the limit is what holds requests back. Without
# it, a CPU backend given more requests than it can answer in time timed out
# the queued ones, and the timeouts tripped the breaker on a healthy backend.
_INITIAL_CONCURRENCY = int(os.environ.get("EMBED_INITIAL_CONCURRENCY", "4"))
_MAX_CONCURRENCY = int(os.environ.get("EMBED_MAX_CONCURRENCY", "32"))
# Times one batch is re-sent to the same backend after a congestion signal
# before the failure counts toward the breaker.
_CONGESTION_RETRIES = 8
# A backend that has answered nothing for this many read timeouts, and whose
# requests have timed out more than _LONE_TIMEOUT_GRACE times in a row when
# sent alone, is not answering, not overloaded. The grace covers the work a
# backend still does for requests that already timed out (it does not cancel
# them); each extra unit costs one read timeout before a hung backend's
# breaker opens.
_SILENT_READ_TIMEOUTS = 2
_LONE_TIMEOUT_GRACE = 3


class _NotAnswering(httpx.ReadTimeout):
    """A read timeout on a backend that has stopped answering: opens its
    breaker at once instead of counting toward the threshold."""


class _BreakerOpen(httpx.TransportError):
    """Raised to a request that waited for a slot on a backend whose breaker
    opened meanwhile, so it fails over instead of being sent. Not a failure
    of the backend: the breaker already counted those."""


@dataclass
class Backend:
    url: str
    klass: BackendClass
    in_flight_tokens: int = 0
    failures: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # --- Circuit breaker state ---
    cb_state: CircuitState = CircuitState.CLOSED
    cb_failure_times: list[float] = field(default_factory=list)  # monotonic timestamps of recent failures
    cb_opened_at: float = 0.0  # monotonic timestamp when tripped to OPEN
    # Backend's advertised max inputs-per-/embed-request, learned from a 422
    # "batch size N > maximum allowed batch size M". None = unknown
    # (send optimistically); once learned, oversized batches are pre-split.
    max_batch_size: int | None = None
    # --- Adaptive concurrency state (see _INITIAL_CONCURRENCY) ---
    concurrency_limit: float = float(_INITIAL_CONCURRENCY)
    active: int = 0  # requests currently sent to this backend
    slow_start_threshold: float = float("inf")
    last_decrease_at: float = float("-inf")  # monotonic; one decrease per congestion event
    last_answer_at: float = float("-inf")  # monotonic; any HTTP response
    sends: int = 0  # requests sent so far; tells a request whether others were sent during it
    lone_timeouts: int = 0  # read timeouts on requests sent alone since the last answer
    _waiters: collections.deque = field(default_factory=collections.deque)


# A TEI backend rejects an oversized /embed with HTTP 422 and a body like
# {"error":"batch size 40 > maximum allowed batch size 32"}. We parse the cap so
# the proxy can split + retry on the SAME backend instead of tripping the
# circuit breaker (the batch is too big, the backend is healthy).
_BATCH_LIMIT_RE = re.compile(r"maximum allowed batch size (\d+)")


def _parse_batch_limit(text: str) -> int | None:
    m = _BATCH_LIMIT_RE.search(text or "")
    return int(m.group(1)) if m else None


def _estimate_batch_tokens(batch: list[str]) -> int:
    return _get_estimator().batch_cost(batch)


def _estimate_max_chunk_tokens(batch: list[str]) -> int:
    return _get_estimator().max_chunk_cost(batch)


class EmbeddingProxy:
    """Least-loaded (by pending token cost) router across TEI backends.

    Backends can be live-reloaded from a Postgres registry; see
    `from_registry()`, `reload()`, and `start_listener()`. The legacy
    `from_env()` path stays for development/CI use without a database.
    """

    def __init__(
        self,
        backends: list[Backend],
        *,
        gpu_max_tokens: int = 4096,
        max_batch_size: int = 128,
        client: httpx.AsyncClient | None = None,
        initial_concurrency: int = _INITIAL_CONCURRENCY,
        max_concurrency: int = _MAX_CONCURRENCY,
    ) -> None:
        if not backends:
            raise ValueError("EmbeddingProxy requires at least one backend")
        self._initial_concurrency = max(1, initial_concurrency)
        self._max_concurrency = max(self._initial_concurrency, max_concurrency)
        for b in backends:
            b.concurrency_limit = float(self._initial_concurrency)
        self._backends = backends
        self._gpu_max_tokens = gpu_max_tokens
        self._max_batch_size = max_batch_size
        self._client = client or httpx.AsyncClient(timeout=120.0)
        # An answer slower than half the read timeout means the backend is
        # close to timing requests out: back off before it does.
        read_timeout = getattr(getattr(self._client, "timeout", None), "read", None)
        self._read_timeout = read_timeout or 0.0
        self._slow_after = read_timeout / 2 if read_timeout else None
        self._select_lock = asyncio.Lock()
        # Set by start_listener; nulls keep stop_listener idempotent.
        self._listener_task: asyncio.Task | None = None
        self._listener_conn = None  # asyncpg.Connection | None
        self._listener_stopping = False

    @classmethod
    def from_env(cls) -> EmbeddingProxy:
        gpu_urls = [u.strip() for u in os.environ.get("EMBEDDING_URLS", "").split(",") if u.strip()]
        if not gpu_urls and (single := os.environ.get("EMBEDDING_URL", "").strip()):
            gpu_urls = [single]
        cpu_urls = [u.strip() for u in os.environ.get("EMBEDDING_FALLBACK_URLS", "").split(",") if u.strip()]
        if not cpu_urls and (single := os.environ.get("EMBEDDING_FALLBACK_URL", "").strip()):
            cpu_urls = [single]

        backends = [Backend(url=u, klass=BackendClass.GPU) for u in gpu_urls]
        backends.extend(Backend(url=u, klass=BackendClass.CPU) for u in cpu_urls)
        return cls(
            backends=backends,
            gpu_max_tokens=int(os.environ.get("GPU_MAX_TOKENS", "4096")),
            max_batch_size=int(os.environ.get("EMBED_BATCH_SIZE", "128")),
        )

    @classmethod
    async def from_registry(cls, store) -> EmbeddingProxy:
        """Build a proxy from the live `embedding_backends` registry."""
        rows = await store.list_enabled()
        backends = [
            Backend(url=r.url, klass=BackendClass(r.klass))
            for r in rows
        ]
        return cls(
            backends=backends,
            gpu_max_tokens=int(os.environ.get("GPU_MAX_TOKENS", "4096")),
            max_batch_size=int(os.environ.get("EMBED_BATCH_SIZE", "128")),
        )

    async def reload(self, store) -> None:
        """Re-read the registry and swap the backend list.

        Carries over `in_flight_tokens` and `failures` for URLs that
        already exist by reusing the same `Backend` instance — that way
        any in-flight request still referencing the old object sees
        consistent state.

        Empty registry keeps the last good config and logs a warning.
        """
        rows = await store.list_enabled()
        if not rows:
            logger.warning(
                "[embed] reload: registry is empty; keeping last good backend list (%d entries)",
                len(self._backends),
            )
            return

        old_by_url = {b.url: b for b in self._backends}
        new_backends: list[Backend] = []
        for r in rows:
            if r.url in old_by_url:
                existing = old_by_url[r.url]
                existing.klass = BackendClass(r.klass)
                new_backends.append(existing)
            else:
                b = Backend(url=r.url, klass=BackendClass(r.klass))
                b.concurrency_limit = float(self._initial_concurrency)
                new_backends.append(b)

        async with self._select_lock:
            self._backends = new_backends
        logger.info("[embed] reloaded backends: %d entries", len(new_backends))

    async def start_listener(self, store, dsn: str) -> None:
        """Spawn a background task that LISTENs on `embedding_backends_changed`.

        Uses a *dedicated* asyncpg connection — the pool's release/reuse
        cycle would break LISTEN bindings. Reconnects on connection drop
        with exponential backoff (cap 30s). Idempotent: a second call
        replaces the running listener.
        """
        await self.stop_listener()
        self._listener_stopping = False
        self._listener_task = asyncio.create_task(
            self._listener_supervisor(store, dsn)
        )

    async def stop_listener(self) -> None:
        self._listener_stopping = True
        task = self._listener_task
        self._listener_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._listener_conn is not None:
            try:
                await self._listener_conn.close()
            except Exception:
                pass
            self._listener_conn = None

    async def _listener_supervisor(self, store, dsn: str) -> None:
        from treeweft.adapters.postgresql.embedding_backend_store import NOTIFY_CHANNEL

        import asyncpg

        backoff = 1.0
        while not self._listener_stopping:
            try:
                self._listener_conn = await asyncpg.connect(dsn)

                def _on_notify(_conn, _pid, _channel, _payload):
                    # The callback runs in the asyncpg connection's task
                    # context; schedule the reload as its own task so we
                    # don't block notifications.
                    asyncio.create_task(self.reload(store))

                await self._listener_conn.add_listener(NOTIFY_CHANNEL, _on_notify)
                logger.info("[embed] listening on Postgres channel %r", NOTIFY_CHANNEL)
                backoff = 1.0  # reset after successful connect

                # Hold the connection open. asyncpg dispatches NOTIFY
                # callbacks on its own reader task — this loop just
                # parks until cancellation or the conn drops.
                while not self._listener_stopping:
                    if self._listener_conn.is_closed():
                        raise ConnectionError("listener connection closed")
                    await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._listener_stopping:
                    break
                logger.warning(
                    "[embed] listener disconnected (%s); reconnecting in %.1fs",
                    exc, backoff,
                )
                try:
                    if self._listener_conn is not None and not self._listener_conn.is_closed():
                        await self._listener_conn.close()
                except Exception:
                    pass
                self._listener_conn = None
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    break
                backoff = min(backoff * 2, 30.0)

    async def _select(self, *, require_cpu: bool, exclude: frozenset[str] = frozenset()) -> Backend | None:
        """Pick the backend with the lowest in-flight token cost.

        Holding the select lock during the read+pick is the cheap way to keep
        two concurrent callers from picking the same idle backend; we release
        before issuing the request.

        `exclude` holds URLs already tried for this batch. A dead backend fails
        instantly, so its in-flight cost stays 0 and it would otherwise keep
        winning the pick until its breaker trips.

        Returns None when no untried backend is eligible (all tripped or tried).
        """
        now = time.monotonic()
        async with self._select_lock:
            candidates = [b for b in self._backends if b.url not in exclude]
            pool = [b for b in candidates if b.klass is BackendClass.CPU] if require_cpu else candidates
            if not pool:
                pool = candidates  # no (untried) CPU — fall through to whatever remains

            # Promote any OPEN backend past its cooldown to HALF_OPEN
            for b in pool:
                if b.cb_state is CircuitState.OPEN and (now - b.cb_opened_at) >= _CB_COOLDOWN_SECONDS:
                    b.cb_state = CircuitState.HALF_OPEN
                    logger.info("[embed] backend %s OPEN→HALF_OPEN (cooldown elapsed)", b.url)

            # Filter: CLOSED or HALF_OPEN are eligible; OPEN is not
            eligible = [b for b in pool if b.cb_state is not CircuitState.OPEN]
            if not eligible:
                return None
            return min(eligible, key=lambda b: b.in_flight_tokens)

    async def _acquire_slot(self, backend: Backend) -> int:
        """Wait until `backend` is under its concurrency limit and take a slot.

        Returns the number of requests in flight on it, this one included.
        """
        if backend.active < int(backend.concurrency_limit) and not backend._waiters:
            backend.active += 1
            return backend.active
        fut = asyncio.get_running_loop().create_future()
        backend._waiters.append(fut)
        try:
            await fut  # _wake takes the slot on our behalf
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self._release_slot(backend)
            raise
        return backend.active

    def _release_slot(self, backend: Backend) -> None:
        backend.active -= 1
        self._wake(backend)

    @staticmethod
    def _wake(backend: Backend) -> None:
        while backend._waiters and backend.active < int(backend.concurrency_limit):
            fut = backend._waiters.popleft()
            if not fut.done():
                backend.active += 1
                fut.set_result(None)

    def _back_off(self, backend: Backend, sent_at: float, why: str) -> None:
        if sent_at < backend.last_decrease_at:
            return  # sent before the last decrease: that one already answered this
        old = backend.concurrency_limit
        backend.concurrency_limit = max(1.0, old / 2)
        backend.slow_start_threshold = backend.concurrency_limit
        backend.last_decrease_at = time.monotonic()
        logger.info(
            "[embed] backend %s is overloaded (%s); concurrency limit %d -> %d",
            backend.url, why, int(old), int(backend.concurrency_limit),
        )

    def _grow(self, backend: Backend) -> None:
        limit = backend.concurrency_limit
        limit += 1.0 if limit < backend.slow_start_threshold else 1.0 / limit
        backend.concurrency_limit = min(float(self._max_concurrency), limit)
        self._wake(backend)

    def _is_load(self, backend: Backend, sent_at: float, sends_at_send: int) -> bool:
        """Whether a read timeout is load (back off, re-send) or the backend
        not answering (count it, and open the breaker).

        Load when other requests were in flight or were sent while this one
        waited, or when the backend answered anything within the last
        _SILENT_READ_TIMEOUTS read timeouts, or for the first
        _LONE_TIMEOUT_GRACE lone timeouts since its last answer (it is still
        working through the requests that timed out). Decided when the
        timeout happens, not at send: the first request of a burst is sent
        before the others.
        """
        if backend.active > 1 or backend.sends != sends_at_send:
            return True
        backend.lone_timeouts += 1
        silent_for = time.monotonic() - backend.last_answer_at
        return (
            silent_for < _SILENT_READ_TIMEOUTS * self._read_timeout
            or backend.lone_timeouts <= _LONE_TIMEOUT_GRACE
        )

    async def _send(self, backend: Backend, batch: list[str]) -> httpx.Response:
        """POST one /embed within the backend's concurrency limit.

        A read timeout that `_is_load` attributes to load, or a 429, backs
        off and re-sends to the same backend, up to _CONGESTION_RETRIES
        times, without counting toward the breaker. Any other read timeout
        raises `_NotAnswering`, which opens the breaker. Connect errors,
        other timeouts and 5xx propagate to the breaker as before.
        """
        attempt = 0
        while True:
            in_flight = await self._acquire_slot(backend)
            if backend.cb_state is CircuitState.OPEN:
                self._release_slot(backend)
                raise _BreakerOpen(f"embedding backend {backend.url} circuit breaker is open")
            filled = in_flight >= int(backend.concurrency_limit)
            sent_at = time.monotonic()
            backend.sends += 1
            sends_at_send = backend.sends
            try:
                resp = await self._client.post(
                    f"{backend.url}/embed",
                    json={"inputs": batch, "normalize": True},
                )
            except httpx.ReadTimeout as exc:
                load = self._is_load(backend, sent_at, sends_at_send)
                self._release_slot(backend)
                if not load:
                    raise _NotAnswering(str(exc) or "timed out") from exc
                if attempt >= _CONGESTION_RETRIES:
                    raise
                attempt += 1
                self._back_off(backend, sent_at, "timed out")
                continue
            except BaseException:
                self._release_slot(backend)
                raise
            self._release_slot(backend)
            backend.last_answer_at = time.monotonic()
            backend.lone_timeouts = 0
            if resp.status_code == 429 and attempt < _CONGESTION_RETRIES:
                attempt += 1
                self._back_off(backend, sent_at, "429")
                await asyncio.sleep(min(1.0, 0.05 * 2**attempt))
                continue
            if resp.status_code < 400:
                latency = backend.last_answer_at - sent_at
                if self._slow_after is not None and latency > self._slow_after:
                    self._back_off(backend, sent_at, f"answered in {latency:.1f}s")
                elif filled:
                    self._grow(backend)
            return resp

    async def _post_split(self, backend: Backend, batch: list[str], limit: int) -> list[list[float]]:
        """Post `batch` to `backend` in order, in chunks of `limit`, concatenating
        the embeddings. Used when a batch exceeds the backend's max-batch-size
; backend.max_batch_size is already set so the per-chunk
        _post_one calls won't re-trigger the 422 split."""
        out: list[list[float]] = []
        for i in range(0, len(batch), limit):
            out.extend(await self._post_one(backend, batch[i : i + limit]))
        return out

    async def _post_one(self, backend: Backend, batch: list[str]) -> list[list[float]]:
        from treeweft.infrastructure.tracing import get_tracer

        # Pre-split if we've already learned this backend's batch ceiling, so we
        # never knowingly send an oversized request.
        if backend.max_batch_size and len(batch) > backend.max_batch_size:
            return await self._post_split(backend, batch, backend.max_batch_size)

        cost = _estimate_batch_tokens(batch)
        backend.in_flight_tokens += cost
        counted = True  # cleared when the 422 split hands the cost to its parts
        with get_tracer("treeweft.tei").start_as_current_span(
            "tei.embed",
            attributes={
                "treeweft.backend_url": backend.url,
                "treeweft.backend_class": backend.klass.value,
                "treeweft.token_cost": cost,
                "treeweft.batch_size": len(batch),
            },
        ):
            try:
                resp = await self._send(backend, batch)
                # Oversized-batch 422: the backend is HEALTHY, the batch is too
                # big. Learn the cap, split, and retry on the SAME backend —
                # don't trip the circuit breaker or fall through to a backend
                # with the same limit.
                if resp.status_code == 422:
                    limit = _parse_batch_limit(resp.text)
                    if limit and len(batch) > limit:
                        backend.max_batch_size = limit
                        logger.warning(
                            "[embed] backend %s rejected batch of %d (max %d); "
                            "learned cap, splitting", backend.url, len(batch), limit,
                        )
                        backend.in_flight_tokens -= cost
                        counted = False
                        return await self._post_split(backend, batch, limit)
                resp.raise_for_status()
                # Success — reset circuit breaker
                async with backend._lock:
                    if backend.cb_state is CircuitState.HALF_OPEN:
                        backend.cb_state = CircuitState.CLOSED
                        backend.cb_failure_times.clear()
                        logger.info("[embed] backend %s HALF_OPEN→CLOSED (probe succeeded)", backend.url)
                    elif backend.cb_state is CircuitState.CLOSED:
                        backend.cb_failure_times.clear()
                return resp.json()
            except _BreakerOpen:
                raise
            except (httpx.HTTPStatusError, httpx.HTTPError) as exc:
                # Track failure with sliding window; trip if threshold exceeded
                now = time.monotonic()
                async with backend._lock:
                    backend.failures += 1
                    backend.cb_failure_times.append(now)
                    # Prune old failures outside the window
                    cutoff = now - _CB_WINDOW_SECONDS
                    backend.cb_failure_times = [t for t in backend.cb_failure_times if t >= cutoff]
                    recent = len(backend.cb_failure_times)
                    if backend.cb_state is CircuitState.CLOSED and isinstance(exc, _NotAnswering):
                        backend.cb_state = CircuitState.OPEN
                        backend.cb_opened_at = now
                        logger.warning(
                            "[embed] backend %s CLOSED→OPEN (not answering: requests "
                            "sent alone time out, last answer %s)",
                            backend.url,
                            "never" if backend.last_answer_at == float("-inf")
                            else f"{now - backend.last_answer_at:.0f}s ago",
                        )
                    elif backend.cb_state is CircuitState.CLOSED and recent >= _CB_FAILURE_THRESHOLD:
                        backend.cb_state = CircuitState.OPEN
                        backend.cb_opened_at = now
                        logger.warning(
                            "[embed] backend %s CLOSED→OPEN (%d failures in %.0fs, threshold=%d)",
                            backend.url, recent, _CB_WINDOW_SECONDS, _CB_FAILURE_THRESHOLD,
                        )
                    elif backend.cb_state is CircuitState.HALF_OPEN:
                        backend.cb_state = CircuitState.OPEN
                        backend.cb_opened_at = now
                        logger.warning("[embed] backend %s HALF_OPEN→OPEN (probe failed)", backend.url)
                raise
            finally:
                if counted:
                    backend.in_flight_tokens -= cost

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        require_cpu = _estimate_max_chunk_tokens(batch) > self._gpu_max_tokens
        tried: set[str] = set()
        last_exc: Exception | None = None

        while len(tried) < len(self._backends):
            backend = await self._select(require_cpu=require_cpu, exclude=frozenset(tried))
            if backend is None:
                if last_exc is not None:
                    break  # every untried backend is tripped — surface the real failure
                # All backends are tripped (OPEN) — wait for cooldown and retry
                logger.warning("[embed] all backends tripped; waiting for cooldown")
                raise RuntimeError("All embedding backends are unavailable (circuit breaker tripped)")
            tried.add(backend.url)
            try:
                return await self._post_one(backend, batch)
            except (httpx.HTTPStatusError, httpx.HTTPError) as exc:
                last_exc = exc
                logger.warning("[embed] backend %s failed (%s); trying next", backend.url, exc)
                continue

        assert last_exc is not None
        raise last_exc

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._max_batch_size):
            out.extend(await self._embed_batch(texts[i : i + self._max_batch_size]))
        return out


MAX_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "128"))

_proxy: EmbeddingProxy | None = None


def set_proxy(proxy: EmbeddingProxy) -> None:
    """Install a process-global proxy. The indexer startup hook calls this
    after building the registry-backed proxy so call sites that use
    `get_proxy()` pick it up — including legacy free functions and tests."""
    global _proxy
    _proxy = proxy


def get_proxy() -> EmbeddingProxy:
    """Return the process-global proxy, falling back to env-based init.

    The fallback exists for tests, scripts, and dev runs without
    Postgres. The indexer service overrides this at startup via
    `set_proxy(await EmbeddingProxy.from_registry(...))`.
    """
    global _proxy
    if _proxy is None:
        _proxy = EmbeddingProxy.from_env()
    return _proxy


EMBEDDING_QUERY_PREFIX = os.environ.get("EMBEDDING_QUERY_PREFIX", "")


async def embed(texts: list[str]) -> list[list[float]]:
    return await get_proxy().embed(texts)


async def embed_query(texts: list[str]) -> list[list[float]]:
    """Embed search queries, applying the model's instruction prefix if configured."""
    if EMBEDDING_QUERY_PREFIX:
        texts = [EMBEDDING_QUERY_PREFIX + t for t in texts]
    return await get_proxy().embed(texts)


class TEIEmbeddingAdapter:
    """EmbeddingPort adapter that delegates to the module-level cost-aware proxy.

    The domain port declares `embed` as sync, but every call site awaits the
    underlying free function, so this adapter matches the actual usage (async).
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await get_proxy().embed(texts)
