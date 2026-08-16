from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
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


class IdempotencyOutcome(StrEnum):
    """What claiming a key found there."""

    RESERVED = "reserved"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class IdempotencyReservation:
    outcome: IdempotencyOutcome
    record: IdempotencyRecord | None = None

    @property
    def may_proceed(self) -> bool:
        return self.outcome is IdempotencyOutcome.RESERVED


class IdempotencyStore(ABC):
    """Replay protection, claimed before the work and confirmed after it.

    The obvious shape — do the work, then write the record — has a window that
    matters when the work is money: if the store is unreachable in the moment
    between the commit and the write, the caller gets a 503 for a movement that
    already happened, and nothing recorded the key, so their retry moves it
    again. Two phases close it. The key is claimed first, so a retry inside the
    TTL finds the claim rather than an empty store.

    ``fingerprint`` is a hash of the request payload: reusing one key with a
    different body is a client bug, and returning the first response for it
    would silently confirm an operation that never ran.
    """

    @abstractmethod
    async def reserve(
        self, key: str, *, fingerprint: str, ttl_seconds: int
    ) -> IdempotencyReservation:
        """Claim the key, atomically, and say what was already there.

        Atomically because this replaces a check-then-act that two concurrent
        requests could both pass. The four outcomes are the whole contract:
        ``RESERVED`` is yours to proceed with, ``IN_PROGRESS`` means another
        caller holds it right now, ``COMPLETED`` carries the original record to
        replay, and ``CONFLICT`` is the same key with a different payload.
        """

    @abstractmethod
    async def confirm(
        self, key: str, *, response: dict[str, object], ttl_seconds: int
    ) -> None:
        """Attach the result to a reservation you hold."""

    @abstractmethod
    async def release(self, key: str) -> None:
        """Give the key back, so a refused request does not poison it.

        Called when the work did not happen — insufficient funds, no such
        wallet, a lost version race. Leaving the reservation would make the
        client wait out the TTL before it could retry something that never ran.
        """
