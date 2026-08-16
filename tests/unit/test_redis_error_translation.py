"""Driver-error translation, gap 4 in docs/SECURITY_CONTROLS.md.

The application layer is forbidden from importing `redis`, so if a driver
exception escapes the adapter nobody can catch it: a brief failover becomes an
unhandled 500 with a driver traceback on the response path. The translation is
the whole reason the port is a port.
"""

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from evenkeel.application.errors import (
    ApplicationErrorCode,
    DependencyUnavailableError,
)
from evenkeel.infrastructure.adapters.redis._errors import translating_redis_errors


@pytest.mark.cwe(755)
async def test_a_connection_failure_becomes_a_typed_503() -> None:
    with pytest.raises(DependencyUnavailableError) as error:
        async with translating_redis_errors("lock.acquire"):
            raise RedisConnectionError("Error 61 connecting to localhost:6379")

    assert error.value.code is ApplicationErrorCode.DEPENDENCY_UNAVAILABLE
    assert error.value.status_code == 503


async def test_a_timeout_becomes_the_same_typed_error() -> None:
    """Every driver failure is one outcome to the caller.

    The distinction between a refused connection and a slow one is an
    infrastructure detail; both mean "retry shortly" to a use case.
    """
    with pytest.raises(DependencyUnavailableError):
        async with translating_redis_errors("rate_limit.consume"):
            raise RedisTimeoutError("timed out")


async def test_the_operation_is_named_in_the_details() -> None:
    with pytest.raises(DependencyUnavailableError) as error:
        async with translating_redis_errors("bulkhead.acquire"):
            raise RedisConnectionError("down")

    assert error.value.details == {
        "dependency": "redis",
        "operation": "bulkhead.acquire",
    }


async def test_the_driver_message_is_not_carried_into_the_response() -> None:
    """A driver message contains the host and port it failed to reach.

    The cause is preserved on `__cause__` for the logs; what the client sees is
    the port-level error and nothing else.
    """
    with pytest.raises(DependencyUnavailableError) as error:
        async with translating_redis_errors("idempotency.get"):
            raise RedisConnectionError("connecting to redis-prod-7.internal:6379")

    rendered = f"{error.value.message} {error.value.details}"
    assert "redis-prod-7.internal" not in rendered
    assert isinstance(error.value.__cause__, RedisConnectionError)


@pytest.mark.cwe(755)
async def test_a_bug_in_the_adapter_keeps_its_own_identity() -> None:
    """Only `RedisError` is translated, and this is the half that matters.

    Catching everything would report a `TypeError` in our own code as an
    infrastructure outage, sending whoever is on call to look at Redis.
    """
    with pytest.raises(TypeError):
        async with translating_redis_errors("lock.acquire"):
            raise TypeError("adapter passed the wrong type")


async def test_the_happy_path_is_transparent() -> None:
    async with translating_redis_errors("lock.acquire"):
        value = 1 + 1

    assert value == 2
