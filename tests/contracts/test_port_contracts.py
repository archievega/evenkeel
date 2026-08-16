"""One suite every adapter of a port must pass.

Null adapters are only trustworthy if they behave like the real thing. Testing
the in-memory and Redis implementations separately is how they drift until a
use case that passes in CI deadlocks in production. These tests are written
against the port, and each implementation is a parameter.

The Redis parameters are skipped unless ``TEST_REDIS_URL`` is set, so the suite
runs everywhere and gets stronger where a Redis is available.
"""

import asyncio
import os
from collections.abc import AsyncIterator

import pytest

from evenkeel.application.ports import (
    BulkheadPolicy,
    BulkheadPort,
    DistributedLockPort,
    IdempotencyRecord,
    IdempotencyStore,
    RateLimiterPort,
    RateLimitPolicy,
)
from evenkeel.infrastructure.adapters.memory.bulkhead import InMemoryBulkhead
from evenkeel.infrastructure.adapters.memory.idempotency import InMemoryIdempotencyStore
from evenkeel.infrastructure.adapters.memory.locking import (
    InMemoryDistributedLock,
    InMemoryRateLimiter,
)

pytestmark = pytest.mark.contract

REDIS_URL = os.getenv("TEST_REDIS_URL")
requires_redis = pytest.mark.skipif(REDIS_URL is None, reason="TEST_REDIS_URL not set")


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
    RECORD = IdempotencyRecord(key="k", fingerprint="f", response={"entry_id": "1"})

    async def test_an_unknown_key_is_absent(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        assert await idempotency_store.get("never-written") is None

    async def test_a_stored_record_round_trips(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        await idempotency_store.put(self.RECORD, ttl_seconds=60)

        found = await idempotency_store.get("k")

        assert found is not None
        assert found.fingerprint == "f"
        assert found.response == {"entry_id": "1"}

    async def test_a_non_positive_ttl_stores_nothing(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        await idempotency_store.put(self.RECORD, ttl_seconds=0)

        assert await idempotency_store.get("k") is None

    async def test_a_record_is_gone_once_its_ttl_elapses(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        await idempotency_store.put(self.RECORD, ttl_seconds=1)
        assert await idempotency_store.get("k") is not None

        await asyncio.sleep(1.2)

        assert await idempotency_store.get("k") is None


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
