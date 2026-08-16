import json

from redis.asyncio import Redis

from evenkeel.application.ports import (
    IdempotencyOutcome,
    IdempotencyRecord,
    IdempotencyReservation,
    IdempotencyStore,
)
from evenkeel.infrastructure.adapters.redis._errors import translating_redis_errors

# Claim the key and report what was there, in one round trip.
#
# `SET NX` alone answers "did I get it" and not "what is there instead", so the
# obvious version follows a failed SET with a GET — and between the two, another
# caller can confirm, expire or replace the entry. The answer would then describe
# a state that no longer exists, which for a money path is the wrong kind of
# nearly right.
_RESERVE_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing == false then
    redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
    return 'reserved'
end
return existing
"""

# Release only what is still a reservation. A completed record must survive:
# the work happened, and forgetting it lets a retry do it again.
_RELEASE_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing == false then
    return 0
end
if string.find(existing, '"response"', 1, true) == nil then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisIdempotencyStore(IdempotencyStore):
    """Cross-replica replay protection, claimed before the work.

    The in-memory store only deduplicates retries that land on the same worker,
    which a load balancer makes unlikely. This is the adapter that makes the
    guarantee real once more than one replica is running.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def reserve(
        self, key: str, *, fingerprint: str, ttl_seconds: int
    ) -> IdempotencyReservation:
        if ttl_seconds <= 0:
            # "Already expired": `SET ... EX 0` is an error, and a reservation
            # nothing can confirm is worse than none.
            return IdempotencyReservation(IdempotencyOutcome.RESERVED)

        claim = json.dumps({"fingerprint": fingerprint}, sort_keys=True)
        async with translating_redis_errors("idempotency.reserve"):
            result = await self._client.eval(
                _RESERVE_SCRIPT, 1, self._key(key), claim, ttl_seconds
            )

        if result == "reserved":
            return IdempotencyReservation(IdempotencyOutcome.RESERVED)

        payload = json.loads(result)
        if payload.get("fingerprint") != fingerprint:
            return IdempotencyReservation(IdempotencyOutcome.CONFLICT)
        if "response" not in payload:
            return IdempotencyReservation(IdempotencyOutcome.IN_PROGRESS)
        return IdempotencyReservation(
            IdempotencyOutcome.COMPLETED,
            IdempotencyRecord(
                key=key,
                fingerprint=payload["fingerprint"],
                response=payload["response"],
            ),
        )

    async def confirm(
        self, key: str, *, response: dict[str, object], ttl_seconds: int
    ) -> None:
        if ttl_seconds <= 0:
            return
        async with translating_redis_errors("idempotency.confirm"):
            raw = await self._client.get(self._key(key))
            if raw is None:
                # The reservation expired while the work ran. Writing the record
                # anyway would be correct, but it would also resurrect a key the
                # TTL deliberately dropped; the caller already has its answer.
                return
            payload = json.loads(raw)
            payload["response"] = response
            await self._client.set(
                self._key(key), json.dumps(payload, sort_keys=True), ex=ttl_seconds
            )

    async def release(self, key: str) -> None:
        async with translating_redis_errors("idempotency.release"):
            await self._client.eval(_RELEASE_SCRIPT, 1, self._key(key))

    @staticmethod
    def _key(key: str) -> str:
        return f"idem:{key}"
