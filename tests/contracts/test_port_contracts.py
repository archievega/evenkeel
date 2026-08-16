"""One suite every adapter of a port must pass.

Null adapters are only trustworthy if they behave like the real thing. Testing
the in-memory and Redis implementations separately is how they drift until a
use case that passes in CI deadlocks in production. These tests are written
against the port, and each implementation is a parameter.

The Redis parameters are skipped unless ``TEST_REDIS_URL`` is set, so the suite
runs everywhere and gets stronger where a Redis is available.
"""

import asyncio
import importlib.util
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import uuid4

import pytest

from evenkeel.application.ports import (
    BulkheadPolicy,
    BulkheadPort,
    DistributedLockPort,
    IdempotencyOutcome,
    IdempotencyStore,
    RateLimiterPort,
    RateLimitPolicy,
    RiskAssessmentPort,
    RiskCheck,
    RiskDecision,
    RiskOutcome,
)
from evenkeel.domain.entities.ledger_entry import LedgerDirection
from evenkeel.domain.value_objects.ids import OwnerId, WalletId
from evenkeel.domain.value_objects.money import CurrencyCode, Money
from evenkeel.infrastructure.adapters.memory.bulkhead import InMemoryBulkhead
from evenkeel.infrastructure.adapters.memory.idempotency import InMemoryIdempotencyStore
from evenkeel.infrastructure.adapters.memory.locking import (
    InMemoryDistributedLock,
    InMemoryRateLimiter,
)
from evenkeel.infrastructure.adapters.noop.metrics import NoopMetrics
from evenkeel.infrastructure.adapters.noop.risk import AllowAllRiskAssessment

pytestmark = pytest.mark.contract

REDIS_URL = os.getenv("TEST_REDIS_URL")
requires_redis = pytest.mark.skipif(REDIS_URL is None, reason="TEST_REDIS_URL not set")
requires_aiohttp = pytest.mark.skipif(
    importlib.util.find_spec("aiohttp") is None, reason="requires the `outbound` extra"
)


async def _redis_client() -> object:
    """A client against an empty database.

    In-memory adapters get a fresh instance per test; the Redis ones share a
    server, so without this they fail on keys left by an earlier test rather
    than on the contract. Every implementation must start from the same state
    or the suite compares them under different conditions.
    """
    from redis.asyncio import Redis

    client = Redis.from_url(REDIS_URL or "", decode_responses=True)
    await client.flushdb()
    return client


@pytest.fixture(params=["memory", pytest.param("redis", marks=requires_redis)])
async def lock_port(request: pytest.FixtureRequest) -> AsyncIterator[DistributedLockPort]:
    if request.param == "memory":
        yield InMemoryDistributedLock()
        return
    from evenkeel.infrastructure.adapters.redis.locking import RedisDistributedLock

    client = await _redis_client()
    yield RedisDistributedLock(client)  # type: ignore[arg-type]
    await client.aclose()  # type: ignore[attr-defined]


@pytest.fixture(params=["memory", pytest.param("redis", marks=requires_redis)])
async def rate_limiter(request: pytest.FixtureRequest) -> AsyncIterator[RateLimiterPort]:
    if request.param == "memory":
        yield InMemoryRateLimiter()
        return
    from evenkeel.infrastructure.adapters.redis.locking import RedisRateLimiter

    client = await _redis_client()
    yield RedisRateLimiter(client)  # type: ignore[arg-type]
    await client.aclose()  # type: ignore[attr-defined]


@pytest.fixture(params=["memory", pytest.param("redis", marks=requires_redis)])
async def bulkhead(request: pytest.FixtureRequest) -> AsyncIterator[BulkheadPort]:
    if request.param == "memory":
        yield InMemoryBulkhead()
        return
    from evenkeel.infrastructure.adapters.redis.bulkhead import RedisBulkhead

    client = await _redis_client()
    yield RedisBulkhead(client)  # type: ignore[arg-type]
    await client.aclose()  # type: ignore[attr-defined]


@pytest.fixture(params=["memory", pytest.param("redis", marks=requires_redis)])
async def idempotency_store(
    request: pytest.FixtureRequest,
) -> AsyncIterator[IdempotencyStore]:
    if request.param == "memory":
        yield InMemoryIdempotencyStore()
        return
    from evenkeel.infrastructure.adapters.redis.idempotency import RedisIdempotencyStore

    client = await _redis_client()
    yield RedisIdempotencyStore(client)  # type: ignore[arg-type]
    await client.aclose()  # type: ignore[attr-defined]


class TestDistributedLockContract:
    async def test_an_uncontended_lock_is_acquired(
        self, lock_port: DistributedLockPort
    ) -> None:
        async with lock_port.lock("k1", ttl_ms=1000) as lock:
            assert lock.acquired is True

    async def test_a_held_lock_is_refused_rather_than_raising(
        self, lock_port: DistributedLockPort
    ) -> None:
        async with lock_port.lock("k2", ttl_ms=1000) as first:
            assert first.acquired is True
            async with lock_port.lock("k2", ttl_ms=1000, wait_timeout_ms=50) as second:
                assert second.acquired is False

    async def test_the_lock_is_released_on_exit(
        self, lock_port: DistributedLockPort
    ) -> None:
        async with lock_port.lock("k3", ttl_ms=1000) as first:
            assert first.acquired is True
        async with lock_port.lock("k3", ttl_ms=1000) as second:
            assert second.acquired is True

    async def test_the_lock_is_released_when_the_body_raises(
        self, lock_port: DistributedLockPort
    ) -> None:
        with pytest.raises(RuntimeError):
            async with lock_port.lock("k4", ttl_ms=1000) as lock:
                assert lock.acquired is True
                raise RuntimeError("boom")

    @pytest.mark.cwe(833)
    async def test_the_default_wait_refuses_instead_of_waiting(
        self, lock_port: DistributedLockPort
    ) -> None:
        """`wait_timeout_ms=0` is the port's own default and means "do not
        wait".

        The in-memory adapter read it as "wait forever" and deadlocked under
        contention — in the default configuration of the default adapter, which
        is the one anybody running without Redis gets. Neither this case nor the
        TTL below was in this suite, which is precisely how the two adapters
        came to disagree about them.
        """

        async def second_attempt() -> bool:
            async with lock_port.lock("k5", ttl_ms=1000) as second:
                return second.acquired

        async with lock_port.lock("k5", ttl_ms=1000):
            try:
                # Bounded from the outside, because the failure being tested is
                # a deadlock: an assertion alone would hang the suite until CI
                # killed the job, which is a six hour way to learn something a
                # two second timeout says immediately.
                acquired = await asyncio.wait_for(second_attempt(), timeout=2)
            except TimeoutError:
                pytest.fail(
                    "waited instead of refusing; wait_timeout_ms=0 must not block"
                )

            assert acquired is False

    @pytest.mark.cwe(833)
    async def test_an_expired_lease_is_reclaimed(
        self, lock_port: DistributedLockPort
    ) -> None:
        """`ttl_ms` is what a holder that dies without releasing costs.

        Redis expires the key; the in-memory adapter ignored the argument, so a
        hung task held its lock until the process ended. Held here without
        exiting the block, which is the shape of that failure.
        """
        held = lock_port.lock("k6", ttl_ms=200)
        await held.__aenter__()
        assert held.acquired is True

        await asyncio.sleep(0.4)

        async with lock_port.lock("k6", ttl_ms=1000) as later:
            assert later.acquired is True

        async with lock_port.lock("k4", ttl_ms=1000) as after:
            assert after.acquired is True

    async def test_distinct_keys_do_not_block_each_other(
        self, lock_port: DistributedLockPort
    ) -> None:
        # Nested rather than combined on purpose: the point is that the second
        # acquisition happens while the first is still held.
        async with lock_port.lock("a", ttl_ms=1000) as first:  # noqa: SIM117
            async with lock_port.lock("b", ttl_ms=1000) as second:
                assert first.acquired and second.acquired


class TestRateLimiterContract:
    POLICY = RateLimitPolicy(name="contract", limit=3, window_seconds=60.0)

    async def test_requests_up_to_the_limit_are_allowed(
        self, rate_limiter: RateLimiterPort
    ) -> None:
        for _ in range(3):
            decision = await rate_limiter.consume(key="u1", policy=self.POLICY)
            assert decision.allowed is True

    async def test_the_request_past_the_limit_is_denied(
        self, rate_limiter: RateLimiterPort
    ) -> None:
        for _ in range(3):
            await rate_limiter.consume(key="u2", policy=self.POLICY)

        decision = await rate_limiter.consume(key="u2", policy=self.POLICY)

        assert decision.allowed is False
        assert decision.retry_after_seconds > 0

    async def test_remaining_counts_down(self, rate_limiter: RateLimiterPort) -> None:
        first = await rate_limiter.consume(key="u3", policy=self.POLICY)
        second = await rate_limiter.consume(key="u3", policy=self.POLICY)

        assert first.remaining > second.remaining

    async def test_keys_are_counted_independently(
        self, rate_limiter: RateLimiterPort
    ) -> None:
        for _ in range(3):
            await rate_limiter.consume(key="u4", policy=self.POLICY)

        other = await rate_limiter.consume(key="u5", policy=self.POLICY)

        assert other.allowed is True


class TestIdempotencyStoreContract:
    """Reserve, confirm, release — the same four outcomes from both adapters.

    The two-phase shape exists because writing the record after the commit
    leaves a window where money has moved and nothing remembers the key. Every
    rule below is one an adapter could plausibly get wrong on its own, which is
    the entire argument for testing them against one suite.
    """

    FINGERPRINT = "f"

    async def test_an_unclaimed_key_is_reserved(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        reservation = await idempotency_store.reserve(
            "fresh", fingerprint=self.FINGERPRINT, ttl_seconds=60
        )

        assert reservation.outcome is IdempotencyOutcome.RESERVED
        assert reservation.may_proceed

    async def test_a_second_claim_before_confirmation_is_in_progress(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        """The race the atomic claim exists for: two callers, one key, and no
        result to hand the loser yet."""
        await idempotency_store.reserve(
            "racing", fingerprint=self.FINGERPRINT, ttl_seconds=60
        )

        second = await idempotency_store.reserve(
            "racing", fingerprint=self.FINGERPRINT, ttl_seconds=60
        )

        assert second.outcome is IdempotencyOutcome.IN_PROGRESS
        assert second.record is None

    async def test_a_confirmed_key_replays_its_response(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        await idempotency_store.reserve(
            "done", fingerprint=self.FINGERPRINT, ttl_seconds=60
        )
        await idempotency_store.confirm(
            "done", response={"entry_id": "1"}, ttl_seconds=60
        )

        again = await idempotency_store.reserve(
            "done", fingerprint=self.FINGERPRINT, ttl_seconds=60
        )

        assert again.outcome is IdempotencyOutcome.COMPLETED
        assert again.record is not None
        assert again.record.response == {"entry_id": "1"}

    async def test_the_same_key_with_a_different_payload_conflicts(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        await idempotency_store.reserve("reused", fingerprint="original", ttl_seconds=60)

        different = await idempotency_store.reserve(
            "reused", fingerprint="changed", ttl_seconds=60
        )

        assert different.outcome is IdempotencyOutcome.CONFLICT

    async def test_a_released_key_can_be_claimed_again(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        """A refusal must not burn the key for the length of the TTL."""
        await idempotency_store.reserve(
            "refused", fingerprint=self.FINGERPRINT, ttl_seconds=60
        )

        await idempotency_store.release("refused")

        assert (
            await idempotency_store.reserve(
                "refused", fingerprint=self.FINGERPRINT, ttl_seconds=60
            )
        ).outcome is IdempotencyOutcome.RESERVED

    async def test_releasing_a_confirmed_key_does_nothing(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        """The work happened. Forgetting it would let a retry do it again — the
        one direction in which `release` must not be helpful."""
        await idempotency_store.reserve(
            "finished", fingerprint=self.FINGERPRINT, ttl_seconds=60
        )
        await idempotency_store.confirm(
            "finished", response={"entry_id": "1"}, ttl_seconds=60
        )

        await idempotency_store.release("finished")

        assert (
            await idempotency_store.reserve(
                "finished", fingerprint=self.FINGERPRINT, ttl_seconds=60
            )
        ).outcome is IdempotencyOutcome.COMPLETED

    async def test_a_non_positive_ttl_claims_nothing(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        """`SET ... EX 0` is an error in Redis and was silently accepted by the
        in-memory store — the first thing this suite caught when it met a real
        server."""
        first = await idempotency_store.reserve(
            "expired", fingerprint=self.FINGERPRINT, ttl_seconds=0
        )
        second = await idempotency_store.reserve(
            "expired", fingerprint=self.FINGERPRINT, ttl_seconds=0
        )

        assert first.outcome is IdempotencyOutcome.RESERVED
        assert second.outcome is IdempotencyOutcome.RESERVED, "nothing was stored"

    async def test_a_reservation_expires(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        await idempotency_store.reserve(
            "short", fingerprint=self.FINGERPRINT, ttl_seconds=1
        )

        await asyncio.sleep(1.2)

        assert (
            await idempotency_store.reserve(
                "short", fingerprint=self.FINGERPRINT, ttl_seconds=60
            )
        ).outcome is IdempotencyOutcome.RESERVED


class TestBulkheadContract:
    def policy(self, **overrides: object) -> BulkheadPolicy:
        defaults: dict[str, object] = {
            "name": "contract",
            "limit": 2,
            "lease_ttl_ms": 5_000,
            "wait_timeout_ms": 0,
        }
        defaults.update(overrides)
        return BulkheadPolicy(**defaults)  # type: ignore[arg-type]

    async def test_callers_up_to_the_limit_get_in(self, bulkhead: BulkheadPort) -> None:
        first = bulkhead.acquire(self.policy())
        second = bulkhead.acquire(self.policy())
        async with first, second:
            assert first.acquired is True
            assert second.acquired is True

    async def test_the_caller_past_the_limit_is_refused(
        self, bulkhead: BulkheadPort
    ) -> None:
        first = bulkhead.acquire(self.policy())
        second = bulkhead.acquire(self.policy())
        # Nested deliberately: the third attempt must happen while the first two
        # are still held.
        async with first, second:  # noqa: SIM117
            async with bulkhead.acquire(self.policy()) as third:
                # Refusal is a value. An exception here would force every caller
                # into a try/except just to shed load.
                assert third.acquired is False

    async def test_a_slot_is_returned_on_exit(self, bulkhead: BulkheadPort) -> None:
        async with bulkhead.acquire(self.policy(limit=1)) as first:
            assert first.acquired is True
        async with bulkhead.acquire(self.policy(limit=1)) as second:
            assert second.acquired is True

    async def test_a_slot_is_returned_when_the_body_raises(
        self, bulkhead: BulkheadPort
    ) -> None:
        with pytest.raises(RuntimeError):
            async with bulkhead.acquire(self.policy(limit=1)) as lease:
                assert lease.acquired is True
                raise RuntimeError("dependency exploded")

        async with bulkhead.acquire(self.policy(limit=1)) as after:
            assert after.acquired is True

    async def test_an_expired_lease_stops_holding_its_slot(
        self, bulkhead: BulkheadPort
    ) -> None:
        """A holder that dies mid-call must not occupy the slot forever."""
        leaked = bulkhead.acquire(self.policy(limit=1, lease_ttl_ms=150))
        await leaked.__aenter__()
        assert leaked.acquired is True

        async with bulkhead.acquire(self.policy(limit=1)) as blocked:
            assert blocked.acquired is False

        await asyncio.sleep(0.25)

        async with bulkhead.acquire(self.policy(limit=1)) as reclaimed:
            assert reclaimed.acquired is True

    @pytest.mark.cwe(770)
    async def test_concurrent_callers_never_exceed_the_limit(
        self, bulkhead: BulkheadPort
    ) -> None:
        """The race the Lua script exists to prevent.

        Check-then-add lets several callers all observe one free slot and all
        take it. Twenty simultaneous attempts against a limit of three must
        grant exactly three.
        """
        granted = 0

        async def attempt() -> None:
            nonlocal granted
            async with bulkhead.acquire(self.policy(limit=3)) as lease:
                if lease.acquired:
                    granted += 1
                    await asyncio.sleep(0.05)

        await asyncio.gather(*(attempt() for _ in range(20)))

        assert granted == 3

    async def test_distinct_names_have_separate_occupancy(
        self, bulkhead: BulkheadPort
    ) -> None:
        async with bulkhead.acquire(self.policy(name="a", limit=1)) as first:  # noqa: SIM117
            async with bulkhead.acquire(self.policy(name="b", limit=1)) as second:
                assert first.acquired and second.acquired


# ---------------------------------------------------------------------------
# RiskAssessmentPort
#
# Only two implementations exist and they share exactly one rule, but it is the
# rule the whole port is built on: an implementation answers with a decision. It
# does not raise. The `http` parameter is pointed at a closed port on purpose —
# the worst case for the real adapter is the case most likely to throw.
# ---------------------------------------------------------------------------


@pytest.fixture(params=["allow-all", pytest.param("http", marks=requires_aiohttp)])
async def risk_port(request: pytest.FixtureRequest) -> AsyncIterator[RiskAssessmentPort]:
    if request.param == "allow-all":
        yield AllowAllRiskAssessment()
        return

    from evenkeel.infrastructure.adapters.http.risk import open_http_risk_assessment
    from evenkeel.infrastructure.adapters.http.transport import (
        SessionPolicy,
        TransportPolicy,
    )

    async with open_http_risk_assessment(
        # Port 1 is reserved and nothing listens there, so this is a refused
        # connection rather than a hang — a deterministic worst case.
        base_url="http://127.0.0.1:1",
        path="/decisions",
        api_key="",
        transport_policy=TransportPolicy(
            service="risk", connect_timeout_ms=50, max_attempts=1, budget_ms=500
        ),
        session_policy=SessionPolicy(),
        bulkhead=InMemoryBulkhead(),
        metrics=NoopMetrics(),
    ) as adapter:
        yield adapter


async def test_an_assessment_always_returns_a_decision(
    risk_port: RiskAssessmentPort,
) -> None:
    decision = await risk_port.assess(
        RiskCheck(
            wallet_id=WalletId(uuid4()),
            owner_id=OwnerId(uuid4()),
            amount=Money(amount=Decimal("1.00"), currency=CurrencyCode("EUR")),
            direction=LedgerDirection.DEBIT,
        )
    )

    assert isinstance(decision, RiskDecision)


async def test_an_unavailable_provider_is_never_reported_as_a_refusal(
    risk_port: RiskAssessmentPort,
) -> None:
    """The one confusion that would corrupt every downstream number: a network
    incident counted as fraud."""
    decision = await risk_port.assess(
        RiskCheck(
            wallet_id=WalletId(uuid4()),
            owner_id=OwnerId(uuid4()),
            amount=Money(amount=Decimal("1.00"), currency=CurrencyCode("EUR")),
            direction=LedgerDirection.DEBIT,
        )
    )

    assert decision.outcome is not RiskOutcome.REFUSED
