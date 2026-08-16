import asyncio
import time
from dataclasses import dataclass, field
from types import TracebackType

from evenkeel.application.ports import (
    DistributedLock,
    DistributedLockPort,
    RateLimitDecision,
    RateLimiterPort,
    RateLimitPolicy,
)


@dataclass(slots=True)
class _Holder:
    """One key's mutex, and when the current holder's lease runs out.

    The Redis adapter sets `PX` on the key, so a holder that dies without
    releasing loses the lock once the TTL elapses. This adapter ignored `ttl_ms`
    entirely — fine until the first task that hangs while holding it, and then
    the two adapters disagree about whether the system recovers on its own.
    """

    primitive: asyncio.Lock = field(default_factory=asyncio.Lock)
    expires_at: float = 0.0

    def lease_expired(self) -> bool:
        return self.primitive.locked() and time.monotonic() >= self.expires_at


class _InProcessLock(DistributedLock):
    def __init__(self, holder: _Holder, *, ttl_ms: int, wait_timeout_ms: int) -> None:
        self._holder = holder
        self._ttl_ms = ttl_ms
        self._wait_timeout_ms = wait_timeout_ms
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    async def __aenter__(self) -> DistributedLock:
        if self._holder.lease_expired():
            # Reclaimed by the TTL rather than by asking the holder, which is
            # what Redis does. A task that hung while holding this is exactly
            # the case both adapters have to survive.
            self._holder.primitive.release()

        if self._wait_timeout_ms <= 0:
            # Zero means "do not wait" — the port's own default, and what Redis
            # does. `timeout=None` meant the opposite, so the default
            # configuration of the default adapter deadlocked under contention
            # instead of refusing. No await between the check and the
            # acquisition, so nothing can take it in between.
            if self._holder.primitive.locked():
                return self
            await self._holder.primitive.acquire()
        else:
            try:
                await asyncio.wait_for(
                    self._holder.primitive.acquire(),
                    timeout=self._wait_timeout_ms / 1000,
                )
            except TimeoutError:
                return self

        self._acquired = True
        self._holder.expires_at = time.monotonic() + self._ttl_ms / 1000
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._acquired:
            return
        self._acquired = False
        # Only if it is still held: an expired lease may already have been
        # reclaimed and handed on, and releasing then would free somebody
        # else's lock. The Redis adapter compares its token for the same reason.
        if self._holder.primitive.locked():
            self._holder.primitive.release()


class InMemoryDistributedLock(DistributedLockPort):
    """Single-process mutual exclusion.

    Correct for one replica and for tests, and wrong the moment a second
    replica exists -- which is exactly why the aggregate also carries a version
    check. Swap in the Redis adapter before scaling out.
    """

    def __init__(self) -> None:
        self._holders: dict[str, _Holder] = {}

    def lock(
        self,
        key: str,
        *,
        ttl_ms: int,
        wait_timeout_ms: int = 0,
        retry_interval_ms: int = 25,
    ) -> DistributedLock:
        holder = self._holders.setdefault(key, _Holder())
        return _InProcessLock(holder, ttl_ms=ttl_ms, wait_timeout_ms=wait_timeout_ms)


class InMemoryRateLimiter(RateLimiterPort):
    """Fixed-window counter, per process."""

    def __init__(self) -> None:
        self._windows: dict[str, tuple[float, int]] = {}

    async def consume(
        self,
        *,
        key: str,
        policy: RateLimitPolicy,
        cost: int = 1,
    ) -> RateLimitDecision:
        now = time.monotonic()
        composite = f"{policy.name}:{key}"
        started_at, used = self._windows.get(composite, (now, 0))
        if now - started_at >= policy.window_seconds:
            started_at, used = now, 0

        if used + cost > policy.limit:
            self._windows[composite] = (started_at, used)
            return RateLimitDecision(
                allowed=False,
                remaining=max(0, policy.limit - used),
                retry_after_seconds=max(0.0, policy.window_seconds - (now - started_at)),
            )

        used += cost
        self._windows[composite] = (started_at, used)
        return RateLimitDecision(
            allowed=True,
            remaining=max(0, policy.limit - used),
            retry_after_seconds=0.0,
        )
