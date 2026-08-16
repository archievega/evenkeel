"""The state machine that decides when to stop calling a dead provider.

Tested directly rather than through the transport because the interesting
behaviour is temporal — open, wait, probe, reopen — and driving it through real
HTTP would mean sleeping through the cooldown in every test.
"""

import time

import pytest

from evenkeel.infrastructure.adapters.http.circuit import (
    CircuitBreaker,
    CircuitPolicy,
    CircuitState,
)


def breaker(**overrides: int) -> CircuitBreaker:
    return CircuitBreaker(
        CircuitPolicy(failure_threshold=3, reset_timeout_ms=50, **overrides)
    )


def test_a_closed_circuit_allows_everything() -> None:
    circuit = breaker()

    assert all(circuit.allows() for _ in range(100))


def test_consecutive_failures_open_it() -> None:
    circuit = breaker()

    for _ in range(3):
        assert circuit.allows()
        circuit.record_failure()

    assert circuit.state is CircuitState.OPEN
    assert circuit.allows() is False


def test_a_success_resets_the_count() -> None:
    """Otherwise a provider with a steady 1% error rate opens the circuit
    eventually, no matter how healthy it is."""
    circuit = breaker()

    circuit.record_failure()
    circuit.record_failure()
    circuit.record_success()
    circuit.record_failure()
    circuit.record_failure()

    assert circuit.state is CircuitState.CLOSED
    assert circuit.allows()


async def test_after_the_cooldown_exactly_one_probe_is_allowed() -> None:
    circuit = breaker()
    for _ in range(3):
        circuit.record_failure()

    _sleep_past(50)

    assert circuit.allows() is True, "the probe must be let through"
    assert circuit.allows() is False, "a second caller must not stampede the provider"


async def test_a_failed_probe_reopens_without_re_earning_the_threshold() -> None:
    circuit = breaker()
    for _ in range(3):
        circuit.record_failure()
    _sleep_past(50)
    assert circuit.allows()

    circuit.record_failure()

    assert circuit.state is CircuitState.OPEN
    assert circuit.allows() is False


async def test_a_successful_probe_closes_it() -> None:
    circuit = breaker()
    for _ in range(3):
        circuit.record_failure()
    _sleep_past(50)
    assert circuit.allows()

    circuit.record_success()

    assert circuit.state is CircuitState.CLOSED
    assert circuit.allows()


@pytest.mark.parametrize("threshold", [1, 2, 5])
def test_the_threshold_is_the_number_configured(threshold: int) -> None:
    circuit = CircuitBreaker(
        CircuitPolicy(failure_threshold=threshold, reset_timeout_ms=1_000)
    )

    for _ in range(threshold - 1):
        circuit.record_failure()
    assert circuit.allows(), "opened early"

    circuit.record_failure()
    assert circuit.allows() is False, "did not open on the configured failure"


def _sleep_past(ms: int) -> None:
    """Blocking, and correct here.

    The breaker reads `time.monotonic()`, so an `asyncio.sleep` would be the
    same wall clock with more machinery. Fifty milliseconds is worth more than
    the fake-clock seam it would take to avoid it.
    """
    time.sleep(ms / 1000 + 0.01)
