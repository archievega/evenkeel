"""`/metrics`: present when asked for, absent otherwise, and honest in between.

The third of those is the one worth testing. A metrics endpoint that exists but
reports nothing looks identical to a healthy one on a dashboard with no data
yet, and the mistake is only found during the incident it was installed for.
"""

from collections.abc import AsyncIterator

import pytest
from dishka import make_async_container
from dishka.integrations.fastapi import FastapiProvider
from httpx import ASGITransport, AsyncClient

from evenkeel.infrastructure.adapters.prometheus.metrics import PrometheusMetrics
from evenkeel.setup.app_factory import create_app
from evenkeel.setup.config import AppConfig, ObservabilityConfig, Settings


def settings_with(*, metrics_enabled: bool) -> Settings:
    return Settings(
        app=AppConfig(environment="local"),
        observability=ObservabilityConfig(metrics_enabled=metrics_enabled),
    )


async def client_for(settings: Settings) -> AsyncClient:
    app = create_app(settings=settings, container=make_async_container(FastapiProvider()))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def instrumented() -> AsyncIterator[AsyncClient]:
    async with await client_for(settings_with(metrics_enabled=True)) as http:
        yield http


async def test_the_endpoint_is_absent_unless_enabled() -> None:
    async with await client_for(settings_with(metrics_enabled=False)) as http:
        assert (await http.get("/metrics")).status_code == 404


async def test_the_endpoint_serves_the_prometheus_format(
    instrumented: AsyncClient,
) -> None:
    response = await instrumented.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


async def test_a_served_request_shows_up_in_the_counters(
    instrumented: AsyncClient,
) -> None:
    await instrumented.get("/health")

    body = (await instrumented.get("/metrics")).text

    assert 'evenkeel_http_requests_total{handler="/health"' in body
    assert 'status="200"' in body


async def test_the_handler_label_is_a_route_template_not_a_path() -> None:
    """One time series per route, not one per wallet id. The failure this
    guards against does not show up until production has enough distinct ids to
    exhaust the monitoring system's memory."""
    metrics = PrometheusMetrics()
    metrics.request_finished(
        method="GET",
        handler="/v1/wallets/{wallet_id}",
        status_code=200,
        duration_seconds=0.01,
    )

    body = _render(metrics)

    assert 'handler="/v1/wallets/{wallet_id}"' in body


def test_external_call_outcomes_are_countable_separately() -> None:
    """The reason this adapter exists: from outside, a shed request, a timeout
    and an exhausted retry budget are all 503."""
    metrics = PrometheusMetrics()
    for outcome in ("success", "timeout", "bulkhead_full", "budget_exhausted"):
        metrics.observe_external_call(
            service="risk", operation="assess", outcome=outcome, duration_seconds=0.1
        )

    body = _render(metrics)

    for outcome in ("success", "timeout", "bulkhead_full", "budget_exhausted"):
        assert f'outcome="{outcome}"' in body


def _render(metrics: PrometheusMetrics) -> str:
    from prometheus_client import generate_latest

    return generate_latest(metrics.registry).decode()
