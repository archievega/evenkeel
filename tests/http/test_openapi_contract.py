"""The published contract must match what the API actually does.

Before this, every operation advertised `200` and `422` and nothing else, while
the code returned 401, 404, 409, 429 and 503 — statuses a generated client had
no branch for. A schema that under-describes the API is not a smaller contract,
it is a wrong one.

These assertions are deliberately about *shape* rather than exact wording, so
they survive rewording and fail on a real regression: an endpoint that stops
declaring a status it still returns.
"""

from typing import Any

import pytest
from dishka import make_async_container
from dishka.integrations.fastapi import FastapiProvider

from evenkeel.setup.app_factory import create_app
from evenkeel.setup.config import AppConfig, Settings

MOVEMENT_PATHS = [
    "/v1/wallets/{wallet_id}/deposits",
    "/v1/wallets/{wallet_id}/withdrawals",
]


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    app = create_app(
        settings=Settings(app=AppConfig(environment="local")),
        container=make_async_container(FastapiProvider()),
    )
    return app.openapi()


def operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, methods in spec["paths"].items()
        for method, operation in methods.items()
    ]


def test_every_operation_is_described(spec: dict[str, Any]) -> None:
    """A renderer can only be as good as what it renders."""
    undocumented = [
        f"{method.upper()} {path}"
        for path, method, operation in operations(spec)
        if not operation.get("summary") or not operation.get("description")
    ]

    assert undocumented == []


@pytest.mark.parametrize("path", MOVEMENT_PATHS)
@pytest.mark.parametrize("status", ["401", "404", "409", "422", "429", "503"])
def test_a_movement_declares_every_status_it_can_return(
    spec: dict[str, Any], path: str, status: str
) -> None:
    """Each of these is produced by a test elsewhere in this suite.

    409 covers insufficient funds, a contended wallet, a lost version race and
    a reused idempotency key; 429 the rate limiter; 503 a Redis outage. All are
    reachable, so all must be declared.
    """
    assert status in spec["paths"][path]["post"]["responses"]


def test_readiness_declares_the_failure_it_is_built_to_report(
    spec: dict[str, Any],
) -> None:
    """A readiness probe that only documents 200 documents nothing."""
    assert "503" in spec["paths"]["/ready"]["get"]["responses"]


def test_every_error_response_is_a_problem_document(spec: dict[str, Any]) -> None:
    """One error shape across the whole surface, and it is the declared one."""
    wrong: list[str] = []
    for path, method, operation in operations(spec):
        for status, response in operation["responses"].items():
            is_error = status.startswith(("4", "5"))
            # /ready answers 503 with its own readiness body, not a problem
            # document — the orchestrator reads it, not an API client.
            readiness_503 = path == "/ready" and status == "503"
            if not is_error or readiness_503:
                continue
            content = response.get("content", {})
            if "application/problem+json" not in content:
                wrong.append(f"{method.upper()} {path} -> {status}")

    assert wrong == []


def test_the_problem_schema_names_the_field_clients_branch_on(
    spec: dict[str, Any],
) -> None:
    problem = spec["components"]["schemas"]["Problem"]

    assert set(problem["required"]) >= {"type", "title", "status", "code", "instance"}
    assert "code" in problem["properties"]


def test_the_api_explains_itself_before_any_endpoint(spec: dict[str, Any]) -> None:
    """The landing text of every renderer.

    Empty here means the reader's first screen is a bare list of paths.
    """
    description = spec["info"].get("description") or ""

    assert "Idempotency-Key" in description
    assert "correlation_id" in description
    assert spec.get("servers")
    assert {tag["name"] for tag in spec["tags"]} == {"wallets", "operations"}


def test_the_request_bodies_carry_examples(spec: dict[str, Any]) -> None:
    """Examples are what make a reference usable without reading the source."""
    without: list[str] = []
    for path, method, operation in operations(spec):
        body = operation.get("requestBody")
        if not body:
            continue
        schema_ref = body["content"]["application/json"]["schema"].get("$ref", "")
        name = schema_ref.rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"].get(name, {})
        if not schema.get("examples"):
            without.append(f"{method.upper()} {path}")

    assert without == []
