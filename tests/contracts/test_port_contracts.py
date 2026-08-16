"""One suite every adapter of a port must pass.

Null adapters are only trustworthy if they behave like the real thing. Testing
the in-memory and Redis implementations separately is how they drift until a
use case that passes in CI deadlocks in production. These tests are written
against the port, and each implementation is a parameter.

The Redis parameters are skipped unless ``TEST_REDIS_URL`` is set, so the suite
runs everywhere and gets stronger where a Redis is available.
"""

import os
from collections.abc import AsyncIterator

import pytest

from appcore.application.ports import (
    DistributedLockPort,
    IdempotencyRecord,
    IdempotencyStore,
    RateLimiterPort,
    RateLimitPolicy,
)
from appcore.infrastructure.adapters.memory.idempotency import InMemoryIdempotencyStore
from appcore.infrastructure.adapters.memory.locking import (
    InMemoryDistributedLock,
    InMemoryRateLimiter,
)

pytestmark = pytest.mark.contract

REDIS_URL = os.getenv("TEST_REDIS_URL")
requires_redis = pytest.mark.skipif(REDIS_URL is None, reason="TEST_REDIS_URL not set")


def _redis_client() -> object:
    from redis.asyncio import Redis

    return Redis.from_url(REDIS_URL or "", decode_responses=True)


@pytest.fixture(params=["memory", pytest.param("redis", marks=requires_redis)])
async def lock_port(request: pytest.FixtureRequest) -> AsyncIterator[DistributedLockPort]:
    if request.param == "memory":
        yield InMemoryDistributedLock()
        return
    from appcore.infrastructure.adapters.redis.locking import RedisDistributedLock

    client = _redis_client()
    yield RedisDistributedLock(client)  # type: ignore[arg-type]
    await client.aclose()  # type: ignore[attr-defined]


@pytest.fixture(params=["memory", pytest.param("redis", marks=requires_redis)])
async def rate_limiter(request: pytest.FixtureRequest) -> AsyncIterator[RateLimiterPort]:
    if request.param == "memory":
        yield InMemoryRateLimiter()
        return
    from appcore.infrastructure.adapters.redis.locking import RedisRateLimiter

    client = _redis_client()
    yield RedisRateLimiter(client)  # type: ignore[arg-type]
    await client.aclose()  # type: ignore[attr-defined]


@pytest.fixture(params=["memory", pytest.param("redis", marks=requires_redis)])
async def idempotency_store(
    request: pytest.FixtureRequest,
) -> AsyncIterator[IdempotencyStore]:
    if request.param == "memory":
        yield InMemoryIdempotencyStore()
        return
    from appcore.infrastructure.adapters.redis.idempotency import RedisIdempotencyStore

    client = _redis_client()
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

    async def test_an_expired_record_is_gone(
        self, idempotency_store: IdempotencyStore
    ) -> None:
        await idempotency_store.put(self.RECORD, ttl_seconds=0)

        assert await idempotency_store.get("k") is None
