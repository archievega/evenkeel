import asyncio
import time
import uuid
from types import TracebackType

from appcore.application.ports.bulkhead import (
    BulkheadLease,
    BulkheadPolicy,
    BulkheadPort,
)


class _InProcessLease(BulkheadLease):
    def __init__(self, holder: "_Occupancy", policy: BulkheadPolicy) -> None:
        self._holder = holder
        self._policy = policy
        self._token = uuid.uuid4().hex
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    async def __aenter__(self) -> BulkheadLease:
        deadline = time.monotonic() + self._policy.wait_timeout_ms / 1000
        while True:
            if self._holder.try_add(self._token, self._policy):
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
        self._holder.remove(self._token)
        self._acquired = False


class _Occupancy:
    """Live leases for one bulkhead name, as token -> expiry."""

    def __init__(self) -> None:
        self._leases: dict[str, float] = {}

    def try_add(self, token: str, policy: BulkheadPolicy) -> bool:
        now = time.monotonic()
        # Reap first, exactly like the Lua script: a caller that died without
        # releasing must not hold its slot forever.
        self._leases = {t: e for t, e in self._leases.items() if e > now}
        if len(self._leases) >= policy.limit:
            return False
        self._leases[token] = now + policy.lease_ttl_ms / 1000
        return True

    def remove(self, token: str) -> None:
        self._leases.pop(token, None)


class InMemoryBulkhead(BulkheadPort):
    """Per-process occupancy, and the default.

    Correct while there is one replica. With N replicas the effective limit is
    N times the configured one, which for a dependency that is being protected
    from overload is the difference between degrading and falling over -- so
    this is the adapter to swap first when scaling out.

    A plain ``asyncio.Semaphore`` would be simpler and would not model lease
    expiry, so the in-memory and Redis adapters would diverge on the one
    behaviour the contract suite is there to pin down.
    """

    def __init__(self) -> None:
        self._occupancy: dict[str, _Occupancy] = {}

    def acquire(self, policy: BulkheadPolicy) -> BulkheadLease:
        holder = self._occupancy.setdefault(policy.name, _Occupancy())
        return _InProcessLease(holder, policy)
