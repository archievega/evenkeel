"""What the middleware promises about labels and headers.

Both properties here were broken and invisible: metric labels collapsed two
endpoints into one series, and error responses carried the correlation header
twice. Neither had a test, and neither shows up in normal use — a duplicated
header reads as a single comma-joined value, and a wrong metric label only
surfaces as a dashboard that quietly means something else.
"""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from dishka import make_async_container
from dishka.integrations.fastapi import FastapiProvider
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Receive, Scope, Send

from evenkeel.domain.value_objects.ids import OwnerId
from evenkeel.infrastructure.adapters.noop.metrics import NoopMetrics
from evenkeel.presentation.http.middleware import ObservabilityMiddleware
from evenkeel.setup.app_factory import create_app
from evenkeel.setup.config import AppConfig, Settings


class RecordingMetrics(NoopMetrics):
    def __init__(self) -> None:
        self.finished: list[tuple[str, str, int]] = []

    def request_finished(
        self, *, method: str, handler: str, status_code: int, duration_seconds: float
    ) -> None:
        self.finished.append((method, handler, status_code))


@pytest.fixture
def metrics() -> RecordingMetrics:
    return RecordingMetrics()


@pytest.fixture
async def client(metrics: RecordingMetrics) -> AsyncIterator[AsyncClient]:
    app = create_app(
        settings=Settings(app=AppConfig(environment="local")),
        container=make_async_container(FastapiProvider()),
        metrics=metrics,
    )

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("internal")

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as http,
    ):
        yield http


async def test_the_metric_label_is_the_full_route_template(
    client: AsyncClient, metrics: RecordingMetrics
) -> None:
    """Not the leaf path the router puts on the scope.

    `scope["route"].path` is relative to the router that owns it, so the
    collection endpoint reports `""` and the item endpoint reports
    `/{wallet_id}` — losing the prefix that tells them apart from any other
    router's routes.
    """
    await client.get("/health")

    assert metrics.finished[-1][1] == "/health"


async def test_two_endpoints_on_one_path_do_not_share_a_label(
    client: AsyncClient, metrics: RecordingMetrics
) -> None:
    await client.get("/health")
    await client.get("/version")

    labels = [handler for _, handler, _ in metrics.finished]
    assert labels == ["/health", "/version"]
    assert len(set(labels)) == 2


async def test_an_unmatched_path_gets_one_bounded_label(
    client: AsyncClient, metrics: RecordingMetrics
) -> None:
    """Scanner traffic must not mint a time series per probed URL."""
    for path in ["/wp-admin", "/.env", "/phpmyadmin"]:
        await client.get(path)

    assert {handler for _, handler, _ in metrics.finished} == {"unmatched"}


async def test_an_error_response_carries_exactly_one_correlation_header(
    client: AsyncClient,
) -> None:
    """Two `X-Correlation-ID` headers reach the client as `"id, id"`.

    The problem document sets the header because the catch-all handler renders
    outside the middleware's wrapper; the wrapper must therefore not add a
    second one when it is already there.
    """
    response = await client.get("/v1/wallets/not-a-uuid")

    values = [v for k, v in response.headers.multi_items() if k == "x-correlation-id"]
    assert len(values) == 1
    assert "," not in values[0]


async def test_a_successful_response_still_carries_the_header(
    client: AsyncClient,
) -> None:
    response = await client.get("/health")

    values = [v for k, v in response.headers.multi_items() if k == "x-correlation-id"]
    assert len(values) == 1


class CountingMetrics(NoopMetrics):
    """Just the three that have to balance."""

    def __init__(self) -> None:
        self.started = 0
        self.finished = 0
        self.failed = 0

    def request_started(self) -> None:
        self.started += 1

    def request_finished(self, **_: object) -> None:
        self.finished += 1

    def request_failed(self, **_: object) -> None:
        self.failed += 1


async def _no_receive() -> Message:
    raise AssertionError("the app under test should not read the body")


async def _discard(message: Message) -> None:
    return None


def _http_scope() -> Scope:
    return {"type": "http", "method": "GET", "path": "/health", "headers": []}


@pytest.mark.parametrize(
    ("failure", "name"),
    [(asyncio.CancelledError, "CancelledError"), (RuntimeError, "RuntimeError")],
)
async def test_every_started_request_is_accounted_for(
    failure: type[BaseException], name: str
) -> None:
    """`CancelledError` inherits `BaseException`, not `Exception`.

    So `except Exception` never saw it: `request_started` was counted, nothing
    was counted against it, and the in-flight gauge climbed by one per
    disconnected client for the life of the process. Harmless while the metrics
    port was a no-op; a lying dashboard the moment it was not.

    Parametrised against an ordinary exception so the test states the invariant
    — started and accounted-for must balance — rather than one special case.
    """
    metrics = CountingMetrics()

    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        raise failure

    middleware = ObservabilityMiddleware(failing, metrics=metrics)

    with pytest.raises(failure):
        await middleware(_http_scope(), _no_receive, _discard)

    assert metrics.started == 1
    assert metrics.finished + metrics.failed == 1, f"{name} left the gauge stranded"


@pytest.mark.cwe(93)
@pytest.mark.parametrize(
    ("supplied", "why"),
    [
        ("abc\r\nX-Injected: yes", "carriage return, so it splits a header"),
        ("abc\nfake log line", "newline, so it writes its own log entry"),
        ("x" * 10_240, "10 KB, copied into every log line for the request"),
        ("abc\x00\x07def", "control characters"),
    ],
)
async def test_a_hostile_correlation_id_is_replaced(
    client: AsyncClient, owner_id: OwnerId, supplied: str, why: str
) -> None:
    """Whatever arrives is echoed into a response header, bound into every log
    line, and forwarded to providers as an outbound header.

    Taken verbatim it was all three at once, unauthenticated: the CRLF reached
    the response headers, the 10 KB reached the logs, and the outbound copy
    raised out of `aiohttp` as a 500. A fresh id is substituted rather than the
    request refused — a malformed trace header is not a reason to fail a call.
    """
    response = await client.get(
        "/v1/wallets",
        headers={
            "Authorization": f"Bearer {owner_id.value}",
            "X-Correlation-Id": supplied,
        },
    )

    echoed = response.headers["x-correlation-id"]
    assert echoed != supplied, why
    assert UUID(echoed)


@pytest.mark.cwe(93)
async def test_a_well_formed_correlation_id_is_kept(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    """The point of the header is that one id spans several systems. Replacing
    a usable one would break the join it exists for."""
    supplied = "018f4b2c-0000-7000-8000-000000000000"

    response = await client.get(
        "/v1/wallets",
        headers={
            "Authorization": f"Bearer {owner_id.value}",
            "X-Correlation-Id": supplied,
        },
    )

    assert response.headers["x-correlation-id"] == supplied
