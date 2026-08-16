import time
from dataclasses import dataclass
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitPolicy:
    """When to stop calling something that is not answering.

    `failure_threshold` counts *consecutive* failures. A count over a window
    would be more forgiving of a provider with a steady 1% error rate, and would
    also take far longer to notice a total outage; consecutive failures is the
    simpler rule and the one that reacts at the speed the incident does.

    `reset_timeout_ms` is how long the circuit stays open before one request is
    allowed through to find out whether the provider came back.
    """

    failure_threshold: int = 5
    reset_timeout_ms: int = 10_000
    half_open_max_calls: int = 1


class CircuitBreaker:
    """Stops a dead dependency from costing a timeout per request.

    Without one, a provider that stopped answering charges every caller the full
    timeout. At 50 requests per second and an 800ms timeout that is 40 requests
    permanently in flight, each holding a task and — because this call sits
    inside a request — a database connection. The bulkhead caps that number; the
    breaker removes the wait entirely once the pattern is unmistakable, and the
    two together are what turn "provider is down" into fast, cheap refusals.

    Deliberately per-process, not shared through Redis. A breaker is a statement
    about what *this* process just observed, and it must keep working when the
    shared store is the thing that is broken. The cost is that N replicas each
    pay their own threshold before opening, which is a rounding error against
    the outage it is reacting to.

    Not thread-safe, and does not need to be: one event loop, no awaits between
    read and write, so no interleaving is possible inside these methods.
    """

    def __init__(self, policy: CircuitPolicy) -> None:
        self._policy = policy
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    def allows(self) -> bool:
        """Ask permission, and count the probe if one is granted.

        Has a side effect on purpose. The half-open state exists to let exactly
        `half_open_max_calls` requests through, which is only enforceable if
        granting permission and taking the slot are the same step — separating
        them lets every caller in the queue observe "half open, room for one"
        in the same tick and stampede the provider that just came back.
        """
        if self._state is CircuitState.CLOSED:
            return True
        if self._state is CircuitState.OPEN:
            if self._elapsed_ms() < self._policy.reset_timeout_ms:
                return False
            self._state = CircuitState.HALF_OPEN
            self._half_open_in_flight = 0
        if self._half_open_in_flight >= self._policy.half_open_max_calls:
            return False
        self._half_open_in_flight += 1
        return True

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
        if self._state is CircuitState.HALF_OPEN:
            # A failed probe reopens immediately and restarts the cooldown.
            # Counting it toward the threshold instead would let a provider
            # that is down oscillate through half-open on every reset.
            self._trip()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._policy.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._consecutive_failures = 0
        self._half_open_in_flight = 0

    def _elapsed_ms(self) -> float:
        return (time.monotonic() - self._opened_at) * 1000
