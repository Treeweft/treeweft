"""Tests for CircuitBreaker domain model."""

import time

import pytest

from treeweft.domain.circuit_breaker import CircuitBreaker, CircuitConfig, CircuitState


class TestCircuitBreaker:
    """Three-state FSM behavior."""

    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert not cb.is_open()

    def test_opens_after_threshold_failures(self):
        config = CircuitConfig(failure_threshold=3, recovery_seconds=99.0)
        cb = CircuitBreaker(config)

        # 2 failures — still closed
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

        # 3rd failure — opens
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.is_open()

    def test_success_resets_failure_count(self):
        config = CircuitConfig(failure_threshold=3, recovery_seconds=99.0)
        cb = CircuitBreaker(config)

        cb.record_failure()
        cb.record_failure()
        cb.record_success()  # resets
        cb.record_failure()
        cb.record_failure()
        # Should still be CLOSED (only 2 failures since last success)
        assert cb.state == CircuitState.CLOSED

    def test_transitions_to_half_open_after_recovery(self):
        config = CircuitConfig(failure_threshold=1, recovery_seconds=0.1)
        cb = CircuitBreaker(config)

        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # Wait for recovery
        time.sleep(0.15)
        # is_open() should transition to HALF_OPEN internally
        assert not cb.is_open()
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_success_closes_circuit(self):
        config = CircuitConfig(failure_threshold=1, recovery_seconds=0.1)
        cb = CircuitBreaker(config)

        cb.record_failure()
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert not cb.is_open()

    def test_half_open_failure_reopens_circuit(self):
        config = CircuitConfig(failure_threshold=1, recovery_seconds=0.1)
        cb = CircuitBreaker(config)

        cb.record_failure()  # OPEN
        time.sleep(0.15)  # → HALF_OPEN
        cb.record_failure()  # back to OPEN
        assert cb.state == CircuitState.OPEN

    def test_reset_clears_everything(self):
        config = CircuitConfig(failure_threshold=1, recovery_seconds=99.0)
        cb = CircuitBreaker(config)

        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_failure_count_below_threshold_stays_closed(self):
        config = CircuitConfig(failure_threshold=5)
        cb = CircuitBreaker(config)

        for _ in range(4):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_custom_threshold(self):
        config = CircuitConfig(failure_threshold=2)
        cb = CircuitBreaker(config)

        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN


class TestAbandonProbe:
    """A HALF_OPEN probe whose call ended with no answer about the provider."""

    def _half_open(self) -> CircuitBreaker:
        cb = CircuitBreaker(CircuitConfig(failure_threshold=1, recovery_seconds=0.05))
        cb.record_failure()
        time.sleep(0.08)
        assert cb.state == CircuitState.HALF_OPEN
        return cb

    def test_unresolved_probe_keeps_the_breaker_rejecting(self):
        cb = self._half_open()

        assert not cb.is_open()  # the one probe is admitted
        assert cb.is_open()      # and until it resolves, nothing else is

    def test_abandoned_probe_lets_the_next_caller_probe(self):
        cb = self._half_open()
        assert not cb.is_open()

        cb.abandon_probe()

        assert cb.state == CircuitState.HALF_OPEN
        assert not cb.is_open()

    def test_abandoning_is_not_a_failure(self):
        cb = CircuitBreaker(CircuitConfig(failure_threshold=2))
        cb.record_failure()

        for _ in range(5):
            cb.abandon_probe()

        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_abandoning_without_a_probe_admits_no_extra_probes(self):
        cb = self._half_open()

        cb.abandon_probe()
        cb.abandon_probe()

        assert not cb.is_open()
        assert cb.is_open()
