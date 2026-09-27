"""LLM Caller — wraps raw _chat() with the full control layer.

CircuitBreaker → RetryEngine → AuditLogger → ResponseValidator → FallbackRouter.

This is the adapter that wires the domain control-layer components into
the existing llm_adapter. Imported and used by generate_hyde() and
generate_summary() to gain resilience without changing their signatures.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Callable, Coroutine, Optional

from treeweft.domain.audit import AuditRecord, FailureMode, Operation
from treeweft.domain.circuit_breaker import CircuitBreaker, CircuitConfig, CircuitState
from treeweft.domain.fallback_router import FallbackRouter
from treeweft.domain.priority_slots import Priority
from treeweft.domain.response_validator import ResponseValidator
from treeweft.domain.retry_engine import RetryConfig, RetryEngine
from treeweft.infrastructure.audit import JSONLAuditLogger

# ── singleton instances (module-level, shared across all callers) ──

from pathlib import Path

from treeweft.adapters.llm_api.llm_adapter import (
    LLM_MODEL,
    LLM_TIMEOUT,
    _chat,
    _HYDE_CACHE,
)

# Circuit breakers, one per operation: 5 consecutive failures → OPEN, 30s
# recovery. Separate, because a HyDE call that runs out of its latency budget
# says nothing about whether a chunk summary would succeed; on a shared
# breaker a burst of searches could make an index job drop its summaries.
_breakers: dict[str, CircuitBreaker] = {}


def _operation_name(operation: Operation) -> str:
    return str(getattr(operation, "value", operation) or "")


def _breaker_for(operation: Operation) -> CircuitBreaker:
    name = _operation_name(operation)
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(
            CircuitConfig(failure_threshold=5, recovery_seconds=30.0)
        )
    return _breakers[name]

# Retry engine: 3 total attempts, 1s base backoff, ±50% jitter
_retry = RetryEngine(
    RetryConfig(max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=30.0)
)

# Audit logger: writes to ~/.treeweft/audit.jsonl
_audit = JSONLAuditLogger(
    Path("~/.treeweft/audit.jsonl"), rebuild=True
)

# Fallback router: strategies registered per operation type
_fallback = FallbackRouter()

# Prompt schemas live in `treeweft.adapters.llm_api.prompts` (the registry,
# ADR-003) as part of each version's `PromptVersion`, not here.


# ── public API ─────────────────────────────────────────────────────


async def call_with_control_layer(
    messages: list[dict],
    max_tokens: int,
    operation: Operation,
    validator: Optional[ResponseValidator] = None,
    audit_id: str = "",
    timeout: Optional[float] = None,
    timeout_includes_queue: bool = True,
    priority: Priority = Priority.BACKGROUND,
) -> tuple[Optional[str], str]:
    """Call the LLM through the full control layer stack.

    Args:
        messages: Chat messages (system + user).
        max_tokens: Max tokens for the LLM response.
        operation: What operation this is (HYDE, SUMMARY, etc.).
        validator: Optional ResponseValidator for output checking.
        audit_id: Optional correlation ID (auto-generated if empty).
        timeout: Optional per-call timeout override.
        timeout_includes_queue: True (default) makes `timeout` the budget
            for the whole call: the wait for an LLM_CONCURRENCY slot, every
            attempt and the backoff between them. An attempt that times out
            has used the budget, so it is not retried, and a failed attempt
            is retried only if half the budget would be left for the next
            one. False bounds each request on its own, from when it gets a
            slot (background work that may queue).
        priority: Who gets a freed LLM_CONCURRENCY slot first. INTERACTIVE
            for a call a user is waiting on; BACKGROUND (default) otherwise.

    Returns:
        (response_text, strategy_name) where strategy_name is:
          - "simple" for first-try success
          - "prompt_mutation" for success after retry
          - "fallback:<name>" for fallback success
          - "error" if everything failed
    """
    input_hash = _hash_messages(messages)
    call_model = LLM_MODEL
    effective_timeout = timeout or LLM_TIMEOUT
    breaker = _breaker_for(operation)
    op_name = _operation_name(operation)
    deadline = (
        time.monotonic() + effective_timeout if timeout_includes_queue else None
    )

    def _time_for_retry(delay_seconds: float) -> bool:
        """Under a budget, retry only if the attempt after the backoff would
        have half the budget: less is a request sent to be abandoned."""
        if deadline is None:
            return True
        left = deadline - (time.monotonic() + delay_seconds)
        return left >= effective_timeout / 2

    # ── Attempt loop ───────────────────────────────────────────────
    for attempt in range(1, _retry._config.max_attempts + 1):
        # 1. Circuit breaker check
        if breaker.is_open():
            _audit.log(
                audit_id=audit_id,
                operation=operation,
                model=call_model,
                attempt=attempt,
                failure_mode=FailureMode.CIRCUIT_OPEN,
                input_hash=input_hash,
            )
            # Route to fallback
            result, strategy, _ = _fallback.execute(
                messages, FailureMode.CIRCUIT_OPEN, attempt
            )
            if result is not None:
                return result, f"fallback:{strategy}"
            return None, "error"

        # 2. Call the LLM
        t0 = time.monotonic()
        sent = False
        timed_out = False

        def _on_slot() -> None:
            nonlocal sent
            sent = True

        try:
            if deadline is not None:
                # What is left of the budget, queue wait included (HyDE).
                raw = await asyncio.wait_for(
                    _chat(
                        messages,
                        max_tokens=max_tokens,
                        operation=op_name,
                        priority=priority,
                        on_slot=_on_slot,
                    ),
                    timeout=max(deadline - t0, 0.0),
                )
            else:
                # Bound only the request, from when an LLM_CONCURRENCY slot
                # is acquired; _chat returns None if it runs over.
                raw = await _chat(
                    messages,
                    max_tokens=max_tokens,
                    operation=op_name,
                    request_timeout=effective_timeout,
                    priority=priority,
                )
        except asyncio.TimeoutError:
            raw = None
            timed_out = True
        except BaseException:
            # Cancelled, or an error from outside the LLM call: nothing was
            # learned about the provider, so neither success nor failure.
            breaker.abandon_probe()
            raise
        latency = (time.monotonic() - t0) * 1000

        # 3. Handle failure (timeout or None)
        if raw is None:
            fm = FailureMode.TIMEOUT if raw is None else FailureMode.LLM_ERROR
            if timed_out and not sent:
                # The budget ran out in the queue for a slot. The provider
                # was never asked, so this says nothing about its health.
                breaker.abandon_probe()
            else:
                breaker.record_failure()
            _audit.log(
                audit_id=audit_id,
                operation=operation,
                model=call_model,
                attempt=attempt,
                failure_mode=fm,
                latency_ms=latency,
                passed=False,
                input_hash=input_hash,
            )
            decision = _retry.evaluate(attempt, fm)
            if decision.should_retry and _time_for_retry(decision.delay_seconds):
                _inject_mutation_hint(messages, decision.mutation_hint)
                await asyncio.sleep(decision.delay_seconds)
                continue
            # Retries or budget exhausted — try fallback
            result, strategy, _ = _fallback.execute(messages, fm, attempt)
            if result is not None:
                return result, f"fallback:{strategy}"
            return None, "error"

        # 4. Validate response
        breaker.record_success()
        if validator is not None:
            validation = validator.validate(raw)
            if not validation.passed:
                _audit.log(
                    audit_id=audit_id,
                    operation=operation,
                    model=call_model,
                    attempt=attempt,
                    failure_mode=validation.failure_mode,
                    latency_ms=latency,
                    passed=False,
                    input_hash=input_hash,
                )
                decision = _retry.evaluate(attempt, validation.failure_mode)
                if decision.should_retry and _time_for_retry(decision.delay_seconds):
                    _inject_mutation_hint(messages, decision.mutation_hint)
                    await asyncio.sleep(decision.delay_seconds)
                    continue
                result, strategy, _ = _fallback.execute(
                    messages, validation.failure_mode, attempt
                )
                if result is not None:
                    return result, f"fallback:{strategy}"
                # The LLM answered every time; the answers were rejected.
                # Distinct from "error" so callers can cache the outcome.
                return None, "rejected"
            raw = validation.cleaned_output

        # 5. Success
        _audit.log(
            audit_id=audit_id,
            operation=operation,
            model=call_model,
            attempt=attempt,
            failure_mode=FailureMode.NONE,
            latency_ms=latency,
            passed=True,
            input_hash=input_hash,
        )
        strategy = "simple" if attempt == 1 else "prompt_mutation"
        return raw, strategy

    # Should never reach here (max_attempts handled above)
    return None, "error"


def _inject_mutation_hint(messages: list[dict], hint: str) -> None:
    """Inject a retry mutation hint into the system message."""
    if not hint:
        return
    for msg in messages:
        if msg.get("role") == "system":
            msg["content"] = f"{msg['content']}\n\n[Hint from previous attempt: {hint}]"
            return
    # No system message — inject as a new one at the start
    messages.insert(0, {"role": "system", "content": f"Hint: {hint}"})


def _hash_messages(messages: list[dict]) -> str:
    """Create a stable hash of the messages for deduplication."""
    raw = "|".join(
        f"{m['role']}:{m['content'][:200]}" for m in messages
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def register_fallback(name: str, strategy) -> None:
    """Register a fallback strategy for the global router.

    Called during application startup wiring. Strategies are tried
    in registration order.

    Example:
        register_fallback("cached_hyde", make_cached_hyde_strategy(cache))
        register_fallback("vector_only", make_vector_only_strategy(search_fn))
    """
    _fallback.register(name, strategy)


def get_audit_stats() -> dict:
    """Get current audit analytics (for /health endpoint)."""
    return _audit.stats()


def get_breaker_state() -> str:
    """The most severe state among the per-operation circuit breakers
    (for /health endpoint): OPEN, else HALF_OPEN, else CLOSED."""
    states = {breaker.state for breaker in _breakers.values()}
    for state in (CircuitState.OPEN, CircuitState.HALF_OPEN):
        if state in states:
            return state.name
    return CircuitState.CLOSED.name


# Re-export singletons for testing
_retry_engine = _retry
_audit_logger = _audit
_fallback_router = _fallback
