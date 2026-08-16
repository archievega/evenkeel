import time
from dataclasses import dataclass

from evenkeel.application.ports import (
    IdempotencyOutcome,
    IdempotencyRecord,
    IdempotencyReservation,
    IdempotencyStore,
)


@dataclass(slots=True)
class _Entry:
    fingerprint: str
    expires_at: float
    response: dict[str, object] | None = None

    @property
    def completed(self) -> bool:
        return self.response is not None


class InMemoryIdempotencyStore(IdempotencyStore):
    """Per-process replay protection with TTL.

    Entries expire lazily on read: a background sweeper would be wasted work for
    a store whose Redis counterpart is the real deployment target.

    Single-threaded with no awaits between read and write, so the claim below is
    atomic for the same reason the Redis one needs a Lua script to be.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    async def reserve(
        self, key: str, *, fingerprint: str, ttl_seconds: int
    ) -> IdempotencyReservation:
        if ttl_seconds <= 0:
            # "Already expired". Claiming it would leave a reservation nothing
            # can confirm; the contract suite pins this on both adapters after
            # Redis rejected `SET ... EX 0` outright.
            return IdempotencyReservation(IdempotencyOutcome.RESERVED)

        existing = self._live(key)
        if existing is None:
            self._entries[key] = _Entry(
                fingerprint=fingerprint, expires_at=time.monotonic() + ttl_seconds
            )
            return IdempotencyReservation(IdempotencyOutcome.RESERVED)

        if existing.fingerprint != fingerprint:
            return IdempotencyReservation(IdempotencyOutcome.CONFLICT)
        if not existing.completed:
            return IdempotencyReservation(IdempotencyOutcome.IN_PROGRESS)
        return IdempotencyReservation(
            IdempotencyOutcome.COMPLETED,
            IdempotencyRecord(
                key=key,
                fingerprint=existing.fingerprint,
                response=existing.response or {},
            ),
        )

    async def confirm(
        self, key: str, *, response: dict[str, object], ttl_seconds: int
    ) -> None:
        if ttl_seconds <= 0:
            return
        existing = self._live(key)
        if existing is None:
            return
        existing.response = response
        existing.expires_at = time.monotonic() + ttl_seconds

    async def release(self, key: str) -> None:
        entry = self._live(key)
        # A completed record is never released: the work happened, and
        # forgetting it would let a retry do it again.
        if entry is not None and not entry.completed:
            del self._entries[key]

    def _live(self, key: str) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            del self._entries[key]
            return None
        return entry
