from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol
from uuid import UUID


class Clock(Protocol):
    """Time as an injected dependency.

    Calling ``datetime.now()`` inside domain or application code makes
    behaviour untestable at boundaries (month rollovers, expiry, DST) and
    couples business rules to the host clock.
    """

    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    """Identity generation as an injected dependency, so tests are deterministic."""

    def generate(self) -> UUID: ...


class TransactionManager(Protocol):
    """The unit of work, owned by the interactor.

    Adapters never commit. Keeping commit/rollback in one place is what makes
    "either the whole use case happened or none of it did" a property you can
    actually reason about.
    """

    # No ``flush()``: flushing is a SQLAlchemy unit-of-work primitive, and
    # exposing it lets application code depend on an ORM detail that a
    # non-ORM adapter cannot honour.
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


class DistributedLock(ABC):
    @property
    @abstractmethod
    def acquired(self) -> bool: ...

    @abstractmethod
    async def __aenter__(self) -> "DistributedLock": ...

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class DistributedLockPort(ABC):
    """Mutual exclusion across processes.

    The in-memory adapter is correct for a single process and is the default,
    so the template runs without Redis; swapping in the Redis adapter is the
    only change needed to make it correct across replicas.
    """

    @abstractmethod
    def lock(
        self,
        key: str,
        *,
        ttl_ms: int,
        wait_timeout_ms: int = 0,
        retry_interval_ms: int = 25,
    ) -> DistributedLock: ...


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    name: str
    limit: int
    window_seconds: float


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: float


class RateLimiterPort(ABC):
    @abstractmethod
    async def consume(
        self,
        *,
        key: str,
        policy: RateLimitPolicy,
        cost: int = 1,
    ) -> RateLimitDecision: ...


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    key: str
    fingerprint: str
    response: dict[str, object]


class IdempotencyStore(ABC):
    """Replay protection for non-idempotent use cases.

    ``fingerprint`` is a hash of the request payload: reusing one key with a
    different body is a client bug, and returning the first response for it
    would silently confirm an operation that never ran.
    """

    @abstractmethod
    async def get(self, key: str) -> IdempotencyRecord | None: ...

    @abstractmethod
    async def put(self, record: IdempotencyRecord, *, ttl_seconds: int) -> None: ...
