import json

from redis.asyncio import Redis

from appcore.application.ports import IdempotencyRecord, IdempotencyStore


class RedisIdempotencyStore(IdempotencyStore):
    """Cross-replica replay protection.

    The in-memory store only deduplicates retries that land on the same worker,
    which a load balancer makes unlikely. This is the adapter that makes the
    guarantee real once more than one replica is running.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def get(self, key: str) -> IdempotencyRecord | None:
        raw = await self._client.get(f"idem:{key}")
        if raw is None:
            return None
        payload = json.loads(raw)
        return IdempotencyRecord(
            key=key,
            fingerprint=payload["fingerprint"],
            response=payload["response"],
        )

    async def put(self, record: IdempotencyRecord, *, ttl_seconds: int) -> None:
        await self._client.set(
            f"idem:{record.key}",
            json.dumps(
                {"fingerprint": record.fingerprint, "response": record.response},
                sort_keys=True,
            ),
            ex=ttl_seconds,
        )
