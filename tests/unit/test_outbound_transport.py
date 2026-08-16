"""The outbound HTTP adapter, against a real server on a real socket.

Not mocked. `aiohttp` is the thing under test here as much as our code is: a
mock cannot tell us whether `sock_read` bounds a dribbling response, whether the
size cap actually stops reading, or whether our timeout is the one that fires.
Every server below is a few lines of `aiohttp.web` on an ephemeral port, which
is cheap enough that there is no excuse for the mock.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from uuid import uuid4

import pytest

aiohttp = pytest.importorskip("aiohttp", reason="requires the `outbound` extra")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from evenkeel.application.ports.risk import RiskCheck, RiskOutcome  # noqa: E402
from evenkeel.domain.entities.ledger_entry import LedgerDirection  # noqa: E402
from evenkeel.domain.value_objects.ids import OwnerId, WalletId  # noqa: E402
from evenkeel.domain.value_objects.money import CurrencyCode, Money  # noqa: E402
from evenkeel.infrastructure.adapters.http.risk import HttpRiskAssessment  # noqa: E402
from evenkeel.infrastructure.adapters.http.transport import (  # noqa: E402
    Failure,
    JsonHttpTransport,
    TransportPolicy,
)
from evenkeel.infrastructure.adapters.memory.bulkhead import (  # noqa: E402
    InMemoryBulkhead,
)
from evenkeel.infrastructure.adapters.noop.metrics import NoopMetrics  # noqa: E402
from tests.fakes.system import FullBulkhead  # noqa: E402

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

PATH = "/decisions"


class CountingHandler:
    """Wraps a handler and remembers how often it ran.

    "Did the retry happen" and "was the circuit skipped" are both questions
    about call count, and asserting on it is the only way to tell a working
    guard from one that silently does nothing.
    """

    def __init__(self, inner: Handler) -> None:
        self._inner = inner
        self.calls = 0
        self.headers: list[dict[str, str]] = []

    async def __call__(self, request: web.Request) -> web.StreamResponse:
        self.calls += 1
        self.headers.append(dict(request.headers))
        return await self._inner(request)


@asynccontextmanager
async def serve(handler: Handler) -> AsyncIterator[tuple[str, CountingHandler]]:
    counting = CountingHandler(handler)

    async def route(request: web.Request) -> web.StreamResponse:
        # A plain coroutine function, not the callable instance: aiohttp
        # inspects what it is given and deprecates anything that is not one.
        return await counting(request)

    app = web.Application()
    app.router.add_post(PATH, route)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/"), counting
    finally:
        await server.close()


def transport(
    session: aiohttp.ClientSession,
    base_url: str,
    *,
    bulkhead: object | None = None,
    **policy: object,
) -> JsonHttpTransport:
    defaults: dict[str, object] = {
        "service": "risk",
        "total_timeout_ms": 300,
        "connect_timeout_ms": 200,
        "read_timeout_ms": 250,
        "budget_ms": 2_000,
        "max_attempts": 2,
        "backoff_base_ms": 1,
        "backoff_max_ms": 2,
    }
    defaults.update(policy)
    return JsonHttpTransport(
        session,
        bulkhead=bulkhead or InMemoryBulkhead(),  # type: ignore[arg-type]
        metrics=NoopMetrics(),
        policy=TransportPolicy(**defaults),  # type: ignore[arg-type]
        base_url=base_url,
    )


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client:
        yield client


def answering(payload: dict[str, object], status: int = 200) -> Handler:
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.json_response(payload, status=status)

    return handler


async def test_a_healthy_provider_answers(session: aiohttp.ClientSession) -> None:
    async with serve(answering({"decision": "allow"})) as (base_url, handler):
        response = await transport(session, base_url).post_json(
            PATH, {}, operation="assess"
        )

        assert response.ok
        assert response.body == {"decision": "allow"}
        assert handler.calls == 1


async def test_a_timeout_is_a_failure_value_not_an_exception(
    session: aiohttp.ClientSession,
) -> None:
    async def slow(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(2)
        return web.json_response({"decision": "allow"})

    async with serve(slow) as (base_url, _):
        response = await transport(
            session, base_url, total_timeout_ms=100, max_attempts=1
        ).post_json(PATH, {}, operation="assess")

        assert response.failure is Failure.TIMEOUT
        assert response.status is None


async def test_a_server_error_is_retried_and_then_reported(
    session: aiohttp.ClientSession,
) -> None:
    async with serve(answering({"error": "boom"}, status=503)) as (base_url, handler):
        response = await transport(session, base_url, max_attempts=3).post_json(
            PATH, {}, operation="assess"
        )

        assert response.failure is Failure.SERVER_ERROR
        assert handler.calls == 3, "every attempt should have reached the provider"


async def test_a_client_error_is_not_retried(session: aiohttp.ClientSession) -> None:
    """A 400 will be a 400 next time too. Retrying it doubles the load on a
    provider that is working, to fix a bug that is ours."""
    async with serve(answering({"error": "bad"}, status=400)) as (base_url, handler):
        response = await transport(session, base_url, max_attempts=3).post_json(
            PATH, {}, operation="assess"
        )

        assert response.failure is Failure.CLIENT_ERROR
        assert handler.calls == 1


async def test_an_oversized_body_is_refused_rather_than_read(
    session: aiohttp.ClientSession,
) -> None:
    async def enormous(request: web.Request) -> web.StreamResponse:
        return web.json_response({"decision": "allow", "padding": "x" * 10_000})

    async with serve(enormous) as (base_url, _):
        response = await transport(
            session, base_url, max_response_bytes=1_000, max_attempts=1
        ).post_json(PATH, {}, operation="assess")

        assert response.failure is Failure.OVERSIZED


async def test_a_body_that_is_not_json_is_malformed_not_a_crash(
    session: aiohttp.ClientSession,
) -> None:
    async def html(request: web.Request) -> web.StreamResponse:
        # What a load balancer returns when the real service is gone, and the
        # single most common shape of "200 OK" that means nothing of the kind.
        return web.Response(text="<html>502 Bad Gateway</html>", content_type="text/html")

    async with serve(html) as (base_url, handler):
        response = await transport(session, base_url, max_attempts=2).post_json(
            PATH, {}, operation="assess"
        )

        assert response.failure is Failure.MALFORMED
        assert handler.calls == 1, "a malformed body will not become valid on retry"


async def test_the_correlation_id_travels_with_the_call(
    session: aiohttp.ClientSession,
) -> None:
    import structlog

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id="known-id")
    try:
        async with serve(answering({"decision": "allow"})) as (base_url, handler):
            await transport(session, base_url).post_json(PATH, {}, operation="assess")

            assert handler.headers[0]["X-Correlation-ID"] == "known-id"
    finally:
        structlog.contextvars.clear_contextvars()


async def test_a_full_bulkhead_refuses_without_calling_the_provider(
    session: aiohttp.ClientSession,
) -> None:
    """The point of the bulkhead: the refusal is instant and the provider never
    hears about the request, so a slow dependency cannot accumulate callers."""
    async with serve(answering({"decision": "allow"})) as (base_url, handler):
        response = await transport(session, base_url, bulkhead=FullBulkhead()).post_json(
            PATH, {}, operation="assess"
        )

        assert response.failure is Failure.BULKHEAD_FULL
        assert handler.calls == 0


async def test_the_budget_bounds_the_worst_case(
    session: aiohttp.ClientSession,
) -> None:
    """Three attempts at 200ms each would be 600ms. The budget says 250ms, and
    the budget is the number anyone quotes."""

    async def slow(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(2)
        return web.json_response({"decision": "allow"})

    async with serve(slow) as (base_url, _):
        client = transport(
            session, base_url, total_timeout_ms=200, budget_ms=250, max_attempts=3
        )
        loop = asyncio.get_running_loop()
        started = loop.time()

        await client.post_json(PATH, {}, operation="assess")

        assert loop.time() - started < 0.5


async def test_an_unrecognised_decision_is_not_permission(
    session: aiohttp.ClientSession,
) -> None:
    """The failure mode this guards against: the provider renames a field, every
    call still returns 200, and the check silently stops existing."""
    async with serve(answering({"verdict": "approved"})) as (base_url, _):
        adapter = HttpRiskAssessment(transport(session, base_url), path=PATH)

        decision = await adapter.assess(_check())

        assert decision.outcome is RiskOutcome.UNAVAILABLE
        assert decision.allowed is False


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"decision": "allow"}, RiskOutcome.ALLOWED),
        ({"decision": "refuse", "reason": "velocity"}, RiskOutcome.REFUSED),
    ],
)
async def test_the_provider_vocabulary_maps_onto_the_port(
    session: aiohttp.ClientSession, body: dict[str, str], expected: RiskOutcome
) -> None:
    async with serve(answering(body)) as (base_url, _):
        adapter = HttpRiskAssessment(transport(session, base_url), path=PATH)

        assert (await adapter.assess(_check())).outcome is expected


async def test_a_dead_provider_is_unavailable_not_refused(
    session: aiohttp.ClientSession,
) -> None:
    """The distinction the whole port exists to preserve. Reported as REFUSED,
    a network incident looks like a fraud spike on every dashboard downstream."""
    adapter = HttpRiskAssessment(
        transport(session, "http://127.0.0.1:1", max_attempts=1, connect_timeout_ms=50),
        path=PATH,
    )

    decision = await adapter.assess(_check())

    assert decision.outcome is RiskOutcome.UNAVAILABLE


def _check() -> RiskCheck:
    return RiskCheck(
        wallet_id=WalletId(uuid4()),
        owner_id=OwnerId(uuid4()),
        amount=Money(amount=Decimal("10.00"), currency=CurrencyCode("EUR")),
        direction=LedgerDirection.DEBIT,
        idempotency_key="key",
    )
