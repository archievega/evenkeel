"""The outbound HTTP adapter, against a real server on a real socket.

Not mocked. `aiohttp` is the thing under test here as much as our code is: a
mock cannot tell us whether `sock_read` bounds a dribbling response, whether the
size cap actually stops reading, or whether our timeout is the one that fires.
Every server below is a few lines of `aiohttp.web` on an ephemeral port, which
is cheap enough that there is no excuse for the mock.
"""

import asyncio
import gzip
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from uuid import uuid4

import pytest

aiohttp = pytest.importorskip("aiohttp", reason="requires the `outbound` extra")

from unittest import mock  # noqa: E402

from aiohttp import ClientConnectionError, web  # noqa: E402
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
    read_capped,
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

    "Did the retry happen" and "was the provider spared entirely" are both
    questions about call count, and asserting on it is the only way to tell a
    working guard from one that silently does nothing.
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


@pytest.mark.cwe(1088)
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


@pytest.mark.cwe(400)
async def test_an_oversized_body_is_refused_rather_than_read(
    session: aiohttp.ClientSession,
) -> None:
    async def endless(request: web.Request) -> web.StreamResponse:
        """Never stops. A body with a known size proves only that the *length
        check* fires — the whole point of the cap is that the read stops, and a
        finite body is consumed either way."""
        response = web.StreamResponse(status=200)
        response.content_type = "application/json"
        await response.prepare(request)
        try:
            while True:
                await response.write(b"x" * 8192)
        except (ConnectionResetError, ClientConnectionError):
            return response

    async with serve(endless) as (base_url, _):
        response = await asyncio.wait_for(
            transport(
                session, base_url, max_response_bytes=1_000, max_attempts=1
            ).post_json(PATH, {}, operation="assess"),
            timeout=5,
        )

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


@pytest.mark.cwe(770)
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


@pytest.mark.cwe(1088)
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


@pytest.mark.cwe(754)
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


@pytest.mark.cwe(755)
async def test_a_response_larger_than_one_segment_is_read_whole() -> None:
    """`StreamReader.read(n)` returns what is buffered, not `n` bytes.

    This shipped: a single read truncated every response over about 1400 bytes,
    which is one TCP segment and roughly two sentences of provider prose. The
    body then failed to parse, the call was reported `MALFORMED`, and a `refuse`
    decision reached the caller as "provider unavailable" — fail-open on the one
    answer that must not be lost.

    It cannot reproduce on a single-segment response, so every other test in
    this file passes with the bug present.
    """
    body = json.dumps(
        {"decision": "refuse", "reason": "x" * 3000, "reference": "r-1"}
    ).encode()

    async def dribbling(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(status=200)
        response.content_type = "application/json"
        await response.prepare(request)
        for start in range(0, len(body), 1400):
            await response.write(body[start : start + 1400])
            await asyncio.sleep(0.01)
        await response.write_eof()
        return response

    async with serve(dribbling) as (base_url, _), aiohttp.ClientSession() as session:
        result = await transport(session, base_url).post_json(
            PATH, {"amount": "1.00"}, operation="assess"
        )

    assert result.ok, f"truncated: {result.failure.value}"
    assert result.body is not None
    assert result.body["decision"] == "refuse"


@pytest.mark.cwe(918)
async def test_a_redirect_is_not_followed(session: aiohttp.ClientSession) -> None:
    """`aiohttp` follows redirects by default, and across hosts.

    So a provider endpoint that can be made to answer `302` reroutes the call —
    payload, API key and all — to whoever wrote the `Location`, and the reply is
    trusted as the provider's. Verified before the fix: the redirect target
    received the request and its `allow` decision was taken.

    This codebase already refuses proxy environment variables on the grounds
    that silently rerouting outbound traffic is a supply-chain problem. A
    followed redirect is the same handover.
    """
    reached: list[str] = []

    async def elsewhere(request: web.Request) -> web.StreamResponse:
        reached.append("target")
        return web.json_response({"decision": "allow"})

    target = web.Application()
    target.router.add_post(PATH, elsewhere)
    server = TestServer(target)
    await server.start_server()

    async def redirecting(request: web.Request) -> web.StreamResponse:
        raise web.HTTPTemporaryRedirect(str(server.make_url(PATH)))

    try:
        async with serve(redirecting) as (base_url, _):
            response = await transport(session, base_url, max_attempts=1).post_json(
                PATH, {}, operation="assess"
            )
    finally:
        await server.close()

    assert response.failure is Failure.REDIRECTED
    assert not reached, "the redirect target must never receive the call"


@pytest.mark.cwe(400)
async def test_the_size_cap_applies_after_decompression(
    session: aiohttp.ClientSession,
) -> None:
    """A cap on the compressed bytes is not a cap. `aiohttp` decompresses
    transparently, so the question is which side of that the limit sits on: 116
    bytes on the wire expand past a 1 KB ceiling here."""
    payload = json.dumps({"decision": "allow", "padding": "x" * 50_000}).encode()

    async def compressed(request: web.Request) -> web.StreamResponse:
        return web.Response(
            body=gzip.compress(payload),
            content_type="application/json",
            headers={"Content-Encoding": "gzip"},
        )

    async with serve(compressed) as (base_url, _):
        response = await transport(
            session, base_url, max_response_bytes=1_000, max_attempts=1
        ).post_json(PATH, {}, operation="assess")

    assert response.failure is Failure.OVERSIZED


@pytest.mark.cwe(400)
async def test_read_capped_stops_at_the_limit() -> None:
    """The endless-body test proves the read terminates. It also passes with the
    cap raised to 64 MB, so the bound needs asserting where it is exact."""
    reader = aiohttp.StreamReader(mock.Mock(_reading_paused=False), limit=2**16)
    reader.feed_data(b"x" * 1_000_000)
    reader.feed_eof()

    raw = await read_capped(reader, 1_001)

    assert len(raw) == 1_001
