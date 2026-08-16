import asyncio
import time
from types import TracebackType

from appcore.application.ports import (
    DistributedLock,
    DistributedLockPort,
    RateLimitDecision,
    RateLimiterPort,
    RateLimitPolicy,
)


class _InProcessLock(DistributedLock):
    def __init__(self, primitive: asyncio.Lock, *, wait_timeout_ms: int) -> None:
        self._primitive = primitive
        self._wait_timeout_ms = wait_timeout_ms
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    async def __aenter__(self) -> DistributedLock:
        try:
            await asyncio.wait_for(
                self._primitive.acquire(),
                timeout=self._wait_timeout_ms / 1000 if self._wait_timeout_ms else None,
            )
            self._acquired = True
        except TimeoutError:
            self._acquired = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._acquired:
            self._primitive.release()
            self._acquired = False


class InMemoryDistributedLock(DistributedLockPort):
    """Single-process mutual exclusion.

    Correct for one replica and for tests, and wrong the moment a second
    replica exists -- which is exactly why the aggregate also carries a version
    check. Swap in the Redis adapter before scaling out.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(
        self,
        key: str,
        *,
        ttl_ms: int,
        wait_timeout_ms: int = 0,
        retry_interval_ms: int = 25,
    ) -> DistributedLock:
        primitive = self._locks.setdefault(key, asyncio.Lock())
        return _InProcessLock(primitive, wait_timeout_ms=wait_timeout_ms)


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
