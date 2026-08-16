"""What an unhandled exception tells the client, gap 1 in docs/SECURITY_CONTROLS.md.

The handler existed and was never asserted. It is the last thing between an
internal failure and the caller, and the failure mode is silent: a handler that
starts leaking the exception message looks exactly like one that does not, right
up until a `psycopg` error puts the DSN in a response body.
"""

from collections.abc import AsyncIterator

import pytest
from dishka import make_async_container
from dishka.integrations.fastapi import FastapiProvider
from httpx import ASGITransport, AsyncClient

from evenkeel.setup.app_factory import create_app
from evenkeel.setup.config import AppConfig, Settings

SECRET_IN_MESSAGE = (
    "connection to server at 10.0.3.7 port 5432 failed: password authentication "
    "failed for user 'evenkeel_prod'"
)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(
        settings=Settings(app=AppConfig()),
        container=make_async_container(FastapiProvider()),
    )

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(SECRET_IN_MESSAGE)

    async with (
        app.router.lifespan_context(app),
        # raise_app_exceptions=False so the transport returns the handler's
        # response instead of re-raising, which is what a real client sees.
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as http,
    ):
        yield http


async def test_an_unhandled_exception_is_a_500(client: AsyncClient) -> None:
    response = await client.get("/boom")

    assert response.status_code == 500


async def test_the_exception_message_never_reaches_the_client(
    client: AsyncClient,
) -> None:
    response = await client.get("/boom")

    body = response.text
    assert SECRET_IN_MESSAGE not in body
    assert "10.0.3.7" not in body
    assert "evenkeel_prod" not in body
    assert "RuntimeError" not in body
    assert "Traceback" not in body


async def test_the_response_carries_a_correlation_id_to_find_the_log(
    client: AsyncClient,
) -> None:
    """Withholding detail is only acceptable with a way back to it.

    The id is what turns "internal server error" into a searchable log line.
    """
    response = await client.get("/boom")

    assert response.json()["correlation_id"]
    assert response.headers["X-Correlation-ID"]


async def test_the_failure_is_still_a_problem_document(client: AsyncClient) -> None:
    response = await client.get("/boom")

    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "INTERNAL_ERROR"
