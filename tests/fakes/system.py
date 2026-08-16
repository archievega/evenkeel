from datetime import UTC, datetime, timedelta
from uuid import UUID

from evenkeel.application.ports import (
    BulkheadLease,
    BulkheadPolicy,
    BulkheadPort,
    DistributedLock,
    DistributedLockPort,
    RateLimitDecision,
    RateLimiterPort,
    RateLimitPolicy,
    RiskAssessmentPort,
    RiskCheck,
    RiskDecision,
    RiskOutcome,
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


class ScriptedRiskAssessment(RiskAssessmentPort):
    """Answers whatever the test needs, and records what it was asked.

    Lives beside the other fakes rather than in one test file: the risk branch
    is exercised from unit tests, HTTP tests and the load-shedding tests, and
    three private copies of this class is how they drift apart.
    """

    def __init__(self, decision: RiskDecision | None = None) -> None:
        self.decision = decision or RiskDecision(outcome=RiskOutcome.ALLOWED)
        self.calls: list[RiskCheck] = []

    async def assess(self, check: RiskCheck) -> RiskDecision:
        self.calls.append(check)
        return self.decision

    @classmethod
    def refusing(cls, *, reference: str = "") -> "ScriptedRiskAssessment":
        return cls(
            RiskDecision(
                outcome=RiskOutcome.REFUSED, reason="scripted", reference=reference
            )
        )

    @classmethod
    def unavailable(cls) -> "ScriptedRiskAssessment":
        return cls(RiskDecision(outcome=RiskOutcome.UNAVAILABLE, reason="scripted"))


class _RefusedLease(BulkheadLease):
    @property
    def acquired(self) -> bool:
        return False

    async def __aenter__(self) -> BulkheadLease:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class FullBulkhead(BulkheadPort):
    """Every slot is taken -- the shed branch, without having to fill it."""

    def acquire(self, policy: BulkheadPolicy) -> BulkheadLease:
        return _RefusedLease()
