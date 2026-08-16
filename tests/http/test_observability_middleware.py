"""What the middleware promises about labels and headers.

Both properties here were broken and invisible: metric labels collapsed two
endpoints into one series, and error responses carried the correlation header
twice. Neither had a test, and neither shows up in normal use — a duplicated
header reads as a single comma-joined value, and a wrong metric label only
surfaces as a dashboard that quietly means something else.
"""

from collections.abc import AsyncIterator

import pytest
from dishka import make_async_container
from dishka.integrations.fastapi import FastapiProvider
from httpx import ASGITransport, AsyncClient

from evenkeel.infrastructure.adapters.noop.metrics import NoopMetrics
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
