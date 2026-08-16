from abc import ABC, abstractmethod
from dataclasses import dataclass
from types import TracebackType


@dataclass(frozen=True, slots=True)
class BulkheadPolicy:
    """How many callers may be inside one dependency at the same time.

    ``lease_ttl_ms`` bounds the damage from a caller that dies without
    releasing: the slot is reclaimed after the TTL instead of being lost for
    the lifetime of the process. Set it above the dependency's own timeout, or
    a slow-but-healthy call will have its lease reclaimed while it is still
    running and the limit will be exceeded.
    """

    name: str
    limit: int
    lease_ttl_ms: int
    wait_timeout_ms: int = 0
    retry_interval_ms: int = 50


class BulkheadLease(ABC):
    """A held slot. Failure to get one is a value, not an exception.

    Same shape as ``DistributedLock``: the caller decides what a refusal means
    -- shed the request, fall back, queue -- instead of having that decision
    made for it by an exception several frames up.
    """

    @property
    @abstractmethod
    def acquired(self) -> bool: ...

    @abstractmethod
    async def __aenter__(self) -> "BulkheadLease": ...

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class BulkheadPort(ABC):
    """Concurrency limiting -- distinct from rate limiting, and not interchangeable.

    A rate limiter caps requests *started* per window. It does nothing about how
    many are *in flight*, so when a dependency slows from 200ms to 20s, a rate
    well under the limit still accumulates hundreds of concurrent calls. Each one
    holds a worker, and — if the call sits inside a request — a database
    connection. The pool empties, and endpoints that never touch the slow
    dependency start failing. The outage looks like a database problem.

    A bulkhead caps concurrent occupancy, so a slow dependency degrades into
    refusals on that one path instead of taking the process down with it.
    """

    @abstractmethod
    def acquire(self, policy: BulkheadPolicy) -> BulkheadLease: ...
