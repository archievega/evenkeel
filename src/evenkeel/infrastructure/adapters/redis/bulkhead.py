import asyncio
import time
import uuid
from types import TracebackType

from redis.asyncio import Redis

from evenkeel.application.ports.bulkhead import (
    BulkheadLease,
    BulkheadPolicy,
    BulkheadPort,
)
from evenkeel.infrastructure.adapters.redis._errors import translating_redis_errors

# A sorted set of live leases scored by expiry. One round trip does all four
# steps, which is what makes the limit real:
#
#   1. TIME comes from Redis, not the caller -- clock skew between application
#      replicas would otherwise make each one reap a different set of leases.
#   2. Expired leases are dropped first, so a crashed holder frees its slot
#      without anyone running a sweeper.
#   3. Occupancy is compared to the limit.
#   4. The new lease is added.
#
# Splitting this into check-then-add lets two callers both observe ZCARD == limit-1
# and both enter -- the exact race the bulkhead exists to prevent.
_ACQUIRE_SCRIPT = """
local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
    return 0
end
redis.call('ZADD', KEYS[1], now_ms + tonumber(ARGV[3]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""

_RELEASE_SCRIPT = """
return redis.call('ZREM', KEYS[1], ARGV[1])
"""


class _RedisLease(BulkheadLease):
    def __init__(self, client: Redis, key: str, policy: BulkheadPolicy) -> None:
        self._client = client
        self._key = key
        self._policy = policy
        self._token = uuid.uuid4().hex
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    async def __aenter__(self) -> BulkheadLease:
        deadline = time.monotonic() + self._policy.wait_timeout_ms / 1000
        while True:
            async with translating_redis_errors("bulkhead.acquire"):
                granted = await self._client.eval(
                    _ACQUIRE_SCRIPT,
                    1,
                    self._key,
                    self._token,
                    self._policy.limit,
                    self._policy.lease_ttl_ms,
                )
            if granted:
                self._acquired = True
                return self
            if time.monotonic() >= deadline:
                return self
            await asyncio.sleep(self._policy.retry_interval_ms / 1000)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._acquired:
            return
        # Releases by token, so a lease that already expired and was reissued to
        # another caller is not removed by its original holder.
        async with translating_redis_errors("bulkhead.release"):
            await self._client.eval(_RELEASE_SCRIPT, 1, self._key, self._token)
        self._acquired = False


class RedisBulkhead(BulkheadPort):
    """Occupancy shared across every replica."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    def acquire(self, policy: BulkheadPolicy) -> BulkheadLease:
        return _RedisLease(self._client, f"bulkhead:{policy.name}", policy)
