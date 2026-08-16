"""The healthy branch of readiness, against a database that really is there."""

from collections.abc import AsyncIterator

import pytest
from dishka import Provider, Scope, from_context, make_async_container
from dishka.integrations.fastapi import FastapiProvider
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from appcore.setup.app_factory import create_app
from appcore.setup.config import Settings

pytestmark = pytest.mark.integration


class RealDatabaseProvider(Provider):
    scope = Scope.APP
    engine = from_context(provides=AsyncEngine)


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    container = make_async_container(
        RealDatabaseProvider(), FastapiProvider(), context={AsyncEngine: engine}
    )
    app = create_app(settings=Settings(), container=container)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        yield http


async def test_readiness_is_ready_when_the_database_answers(
    client: AsyncClient,
) -> None:
    """Proves the probe actually queries.

    A hardcoded `{"status": "ready"}` would pass every unit test ever written
    for it; only a real connection distinguishes a probe from a constant.
    """
    response = await client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["dependencies"]["database"] == {"healthy": True, "error": None}
