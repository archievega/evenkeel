from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.exceptions import RedisError

from appcore.application.errors import (
    ApplicationErrorCode,
    DependencyUnavailableError,
)


@asynccontextmanager
async def translating_redis_errors(operation: str) -> AsyncIterator[None]:
    """Convert driver exceptions into a port-level error.

    An adapter exists to hide which technology is behind a port. If
    `redis.exceptions.ConnectionError` escapes, every caller that wants to
    handle an outage has to import `redis` — and application code is forbidden
    from doing that, so in practice nobody handles it and a brief failover
    becomes a 500 with a driver traceback in the response path.

    Only `RedisError` is caught. A `TypeError` from this adapter is a bug in
    this adapter and must keep its own identity rather than being reported as
    an infrastructure outage.
    """
    try:
        yield
    except RedisError as exc:
        raise DependencyUnavailableError(
            ApplicationErrorCode.DEPENDENCY_UNAVAILABLE,
            details={"dependency": "redis", "operation": operation},
        ) from exc
