from datetime import UTC, datetime, timedelta
from uuid import UUID

from appcore.application.ports import (
    DistributedLock,
    DistributedLockPort,
    RateLimitDecision,
    RateLimiterPort,
    RateLimitPolicy,
)


class FixedClock:
    """Deterministic time. Advances only when a test says so."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class SequentialIdGenerator:
    """Predictable ids, so assertions can name the entity they expect."""

    def __init__(self) -> None:
        self._counter = 0

    def generate(self) -> UUID:
        self._counter += 1
        return UUID(int=self._counter)


class _UnavailableLock(DistributedLock):
    @property
    def acquired(self) -> bool:
        return False

    async def __aenter__(self) -> DistributedLock:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class BusyLock(DistributedLockPort):
    """Every acquisition times out -- exercises the contended branch."""

    def lock(
        self,
        key: str,
        *,
        ttl_ms: int,
        wait_timeout_ms: int = 0,
        retry_interval_ms: int = 25,
    ) -> DistributedLock:
        return _UnavailableLock()


class DenyingRateLimiter(RateLimiterPort):
    async def consume(
        self, *, key: str, policy: RateLimitPolicy, cost: int = 1
    ) -> RateLimitDecision:
        return RateLimitDecision(allowed=False, remaining=0, retry_after_seconds=42.0)
