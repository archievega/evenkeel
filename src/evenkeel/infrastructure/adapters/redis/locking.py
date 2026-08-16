import asyncio
import time
import uuid
from types import TracebackType

from redis.asyncio import Redis

from evenkeel.application.ports import (
    DistributedLock,
    DistributedLockPort,
    RateLimitDecision,
    RateLimiterPort,
    RateLimitPolicy,
)
from evenkeel.infrastructure.adapters.redis._errors import translating_redis_errors

# Releasing a lock is compare-and-delete, never a bare DEL: if the TTL expired
# and another worker already took the lock, a bare DEL would free someone
# else's lock and let two workers into the critical section at once.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

# Fixed window as one atomic INCR + conditional EXPIRE. Doing this as separate
# round trips lets a crash between them leave a key with no TTL, which bans the
# caller permanently.
_CONSUME_SCRIPT = """
local current = redis.call('incrby', KEYS[1], ARGV[1])
if current == tonumber(ARGV[1]) then
    redis.call('pexpire', KEYS[1], ARGV[2])
end
local ttl = redis.call('pttl', KEYS[1])
return {current, ttl}
"""


class _RedisLock(DistributedLock):
    def __init__(
        self,
        client: Redis,
        key: str,
        *,
        ttl_ms: int,
        wait_timeout_ms: int,
        retry_interval_ms: int,
    ) -> None:
        self._client = client
        self._key = key
        self._ttl_ms = ttl_ms
        self._wait_timeout_ms = wait_timeout_ms
        self._retry_interval_ms = retry_interval_ms
        self._token = uuid.uuid4().hex
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    async def __aenter__(self) -> DistributedLock:
        deadline = time.monotonic() + self._wait_timeout_ms / 1000
        while True:
            async with translating_redis_errors("lock.acquire"):
                granted = await self._client.set(
                    self._key, self._token, px=self._ttl_ms, nx=True
                )
            if granted:
                self._acquired = True
                return self
            if time.monotonic() >= deadline:
                return self
            await asyncio.sleep(self._retry_interval_ms / 1000)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._acquired:
            return
        async with translating_redis_errors("lock.release"):
            await self._client.eval(_RELEASE_SCRIPT, 1, self._key, self._token)
        self._acquired = False


class RedisDistributedLock(DistributedLockPort):
    def __init__(self, client: Redis) -> None:
        self._client = client

    def lock(
        self,
        key: str,
        *,
        ttl_ms: int,
        wait_timeout_ms: int = 0,
        retry_interval_ms: int = 25,
    ) -> DistributedLock:
        return _RedisLock(
            self._client,
            f"lock:{key}",
            ttl_ms=ttl_ms,
            wait_timeout_ms=wait_timeout_ms,
            retry_interval_ms=retry_interval_ms,
        )


class RedisRateLimiter(RateLimiterPort):
    def __init__(self, client: Redis) -> None:
        self._client = client

    async def consume(
        self,
        *,
        key: str,
        policy: RateLimitPolicy,
        cost: int = 1,
    ) -> RateLimitDecision:
        window_ms = int(policy.window_seconds * 1000)
        async with translating_redis_errors("rate_limit.consume"):
            current, ttl_ms = await self._client.eval(
                _CONSUME_SCRIPT, 1, f"rl:{policy.name}:{key}", cost, window_ms
            )
        retry_after = max(0.0, ttl_ms / 1000) if ttl_ms > 0 else 0.0
        if current > policy.limit:
            return RateLimitDecision(
                allowed=False, remaining=0, retry_after_seconds=retry_after
            )
        return RateLimitDecision(
            allowed=True,
            remaining=max(0, policy.limit - current),
            retry_after_seconds=0.0,
        )
