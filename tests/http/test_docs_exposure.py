"""Interactive docs outside a local run, gap 2 in docs/SECURITY_CONTROLS.md.

`/docs`, `/redoc`, `/scalar` and `/openapi.json` describe every endpoint, every
field and every error shape. That is a gift to a developer and a map to anyone else. The switch
is one ternary in the app factory, which is precisely the kind of line that gets
inverted during a debugging session and never put back.
"""

from collections.abc import AsyncIterator

import pytest
from dishka import make_async_container
from dishka.integrations.fastapi import FastapiProvider
from httpx import ASGITransport, AsyncClient

from evenkeel.setup.app_factory import create_app
from evenkeel.setup.config import AppConfig, Settings

DOC_PATHS = ["/docs", "/redoc", "/openapi.json", "/scalar"]


async def client_for(*, environment: str) -> AsyncClient:
    app = create_app(
        settings=Settings(app=AppConfig(environment=environment)),
        container=make_async_container(FastapiProvider()),
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def production() -> AsyncIterator[AsyncClient]:
    async with await client_for(environment="production") as http:
        yield http


@pytest.fixture
async def development() -> AsyncIterator[AsyncClient]:
    async with await client_for(environment="local") as http:
        yield http


@pytest.mark.parametrize("path", DOC_PATHS)
async def test_docs_are_absent_outside_local(production: AsyncClient, path: str) -> None:
    response = await production.get(path)

    assert response.status_code == 404


@pytest.mark.parametrize("path", DOC_PATHS)
async def test_docs_are_present_locally(development: AsyncClient, path: str) -> None:
    """The other half, so the test fails if someone disables docs everywhere.

    A control test that only asserts absence passes when the feature is simply
    broken, and then nobody notices the local workflow is gone.
    """
    response = await development.get(path)

    assert response.status_code == 200


async def test_the_health_endpoints_stay_available_in_production(
    production: AsyncClient,
) -> None:
    """Closing the docs must not close the probes.

    They live at the root, unversioned, and the orchestrator needs them
    regardless of environment.
    """
    assert (await production.get("/health")).status_code == 200
    assert (await production.get("/version")).status_code == 200
