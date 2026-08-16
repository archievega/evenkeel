import time

from appcore.application.ports import IdempotencyRecord, IdempotencyStore


class InMemoryIdempotencyStore(IdempotencyStore):
    """Per-process replay protection with TTL.

    Entries expire lazily on read: a background sweeper would be wasted work
    for a store whose Redis counterpart is the real deployment target.
    """

    def __init__(self) -> None:
        self._records: dict[str, tuple[float, IdempotencyRecord]] = {}

    async def get(self, key: str) -> IdempotencyRecord | None:
        found = self._records.get(key)
        if found is None:
            return None
        expires_at, record = found
        if expires_at <= time.monotonic():
            del self._records[key]
            return None
        return record

    async def put(self, record: IdempotencyRecord, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._records[record.key] = (time.monotonic() + ttl_seconds, record)
