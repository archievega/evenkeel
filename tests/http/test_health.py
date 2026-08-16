"""The probes, including the branch that only runs during an outage.

A readiness check is written once and then only ever exercised on the worst day
of the quarter. If the degraded path is never tested, "returns 503 when the
database is gone" is a belief, not a behaviour — and the failure mode is silent:
the load balancer keeps sending traffic to a replica that cannot serve it.
"""

from collections.abc import AsyncIterator

import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import FastapiProvider
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from appcore.setup.app_factory import create_app
from appcore.setup.config import Settings


class UnreachableDatabaseProvider(Provider):
    """An engine pointed at a closed port.

    A real engine, not a mock: `create_async_engine` does not connect eagerly,
    so the failure happens where it would in production — inside `connect()`,
    with a driver exception the probe has to classify itself.
    """

    scope = Scope.APP

    @provide
    async def engine(self) -> AsyncIterator[AsyncEngine]:
        engine = create_async_engine(
            "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nothing"
        )
        yield engine
        await engine.dispose()


@pytest.fixture
async def degraded_client() -> AsyncIterator[AsyncClient]:
    container = make_async_container(UnreachableDatabaseProvider(), FastapiProvider())
    app = create_app(settings=Settings(), container=container)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def test_liveness_ignores_dependencies(degraded_client: AsyncClient) -> None:
    """Liveness must stay green while the database is unreachable.

    Otherwise the orchestrator restarts every healthy replica during a database
    blip and turns a partial outage into a total one.
    """
    response = await degraded_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_readiness_reports_503_when_the_database_is_gone(
    degraded_client: AsyncClient,
) -> None:
    response = await degraded_client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["database"]["healthy"] is False


async def test_readiness_does_not_leak_connection_details(
    degraded_client: AsyncClient,
) -> None:
    """The probe names the failure class, never the driver message.

    Driver errors carry the host, port and user from the DSN, and a readiness
    endpoint is usually reachable from further away than the API itself.
    """
    response = await degraded_client.get("/ready")

    body = str(response.json())
    assert "nobody" not in body
    assert "127.0.0.1" not in body
    assert "nothing" not in body


async def test_version_reports_what_is_deployed(degraded_client: AsyncClient) -> None:
    response = await degraded_client.get("/version")

    assert response.status_code == 200
    assert set(response.json()) == {"version", "commit", "built_at"}
