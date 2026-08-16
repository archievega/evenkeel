#!/usr/bin/env python3
"""Scaffold a vertical: interactor, router, schema, tests, in the right layers.

    make new-vertical NAME=orders

Adding a feature here means touching five files across four layers in a
particular order, and getting any of them wrong is a lint failure at best and a
layer violation at worst. That knowledge should not live only in a document
somebody has to read — this writes the skeleton, and the skeleton compiles.

What it deliberately does **not** do is edit existing files. Rewriting
`setup/ioc/providers/` by regex is the kind of clever that works for six months
and then silently mangles a provider; the closing checklist prints the exact
edits instead, which is also the part a human should be looking at.

The generated code is checked by `tests/unit/test_new_vertical.py`, which runs
the generator and lints its output — a scaffolder that emits code the repository
would reject is worse than none, and rots invisibly otherwise.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "evenkeel"


def singular(name: str) -> str:
    return name[:-1] if name.endswith("s") else name


def camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def interactor_module(area: str) -> str:
    one = singular(area)
    entity = camel(one)
    return f'''import time
from dataclasses import dataclass

from evenkeel.application.errors import ApplicationErrorCode, NotFoundError
from evenkeel.application.ports import MetricsPort
from evenkeel.domain.value_objects.ids import OwnerId


@dataclass(frozen=True, slots=True)
class Get{entity}Query:
    """Only data. Never a port, never a service, never a session."""

    {one}_id: str
    # Ownership travels with the query so the repository can filter in SQL.
    # A check performed after the read is one somebody can delete (ADR 3).
    owner_id: OwnerId


@dataclass(frozen=True, slots=True)
class {entity}Result:
    {one}_id: str


class Get{entity}Interactor:
    """One use case. Policy here, persistence behind a port, rules in the domain.

    TODO({area}): take the repository port this needs as a constructor argument
    and add it to `application/ports/`. Do not import an adapter.
    """

    def __init__(self, metrics: MetricsPort) -> None:
        self._metrics = metrics

    async def __call__(self, query: Get{entity}Query) -> {entity}Result:
        started_at = time.perf_counter()
        outcome = "error"
        try:
            # TODO({area}): read through the port, scoped by `query.owner_id`.
            found = None
            if found is None:
                outcome = "not_found"
                raise NotFoundError(
                    ApplicationErrorCode.VALIDATION_FAILED,
                    details={{"{one}_id": query.{one}_id}},
                )
            outcome = "success"
            return {entity}Result({one}_id=query.{one}_id)
        finally:
            # In `finally`, so a refusal is measured as carefully as a success.
            self._metrics.observe_interactor(
                name="get_{one}",
                outcome=outcome,
                duration_seconds=time.perf_counter() - started_at,
            )
'''


def interactor_init(area: str) -> str:
    entity = camel(singular(area))
    return f'''from evenkeel.application.interactors.{area}.get_{singular(area)} import (
    Get{entity}Interactor,
    Get{entity}Query,
    {entity}Result,
)

__all__ = [
    "Get{entity}Interactor",
    "Get{entity}Query",
    "{entity}Result",
]
'''


def schema_module(area: str) -> str:
    entity = camel(singular(area))
    one = singular(area)
    return f'''from pydantic import BaseModel, Field


class {entity}Response(BaseModel):
    """The shape a client sees. Pydantic lives at the edge and nowhere else."""

    {one}_id: str = Field(description="TODO({area}): describe every field.")
'''


def router_module(area: str) -> str:
    entity = camel(singular(area))
    one = singular(area)
    return f'''from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter

from evenkeel.application.interactors.{area} import (
    Get{entity}Interactor,
    Get{entity}Query,
)
from evenkeel.presentation.http.dependencies import CurrentPrincipal
from evenkeel.presentation.http.responses import read_responses
from evenkeel.presentation.http.schemas.v1.{area} import {entity}Response

router = APIRouter(route_class=DishkaRoute)


@router.get(
    "/{{{one}_id}}",
    response_model={entity}Response,
    responses=read_responses(),
    summary="Read one {one}",
)
async def get_{one}(
    {one}_id: str,
    principal: CurrentPrincipal,
    interactor: FromDishka[Get{entity}Interactor],
) -> {entity}Response:
    """Thin on purpose: parse, delegate, translate. No rules here.

    Errors are not caught — `ApplicationError` and `DomainError` are rendered as
    RFC 9457 problem documents by the single handler in `http/errors.py`.
    """
    result = await interactor(
        Get{entity}Query({one}_id={one}_id, owner_id=principal.owner_id)
    )
    return {entity}Response({one}_id=result.{one}_id)
'''


def test_module(area: str) -> str:
    one = singular(area)
    return f'''"""HTTP tests for the {area} vertical.

Through the real application over fake adapters, like the rest of `tests/http`:
same routers, same middleware, same error handlers. Only the outermost adapters
differ, which is the seam ports exist to provide.
"""

from httpx import AsyncClient

from evenkeel.domain.value_objects.ids import OwnerId


def auth(owner_id: OwnerId) -> dict[str, str]:
    return {{"Authorization": f"Bearer {{owner_id.value}}"}}


async def test_an_unknown_{one}_is_a_404(client: AsyncClient, owner_id: OwnerId) -> None:
    response = await client.get("/v1/{area}/does-not-exist", headers=auth(owner_id))

    assert response.status_code == 404


async def test_a_request_without_credentials_is_rejected(client: AsyncClient) -> None:
    response = await client.get("/v1/{area}/anything")

    assert response.status_code == 401


# TODO({area}): the tests that matter are the ones about *this* vertical's rules.
# A 404 and a 401 are inherited from the framework; what needs proving is the
# behaviour somebody could get wrong.
'''


CHECKLIST = """
Written. Three edits are left, and they are the ones worth a human's eyes:

  1. setup/ioc/providers/{area}.py — register the interactor and its ports:

         class {entity}Provider(Provider):
             scope = Scope.REQUEST
             interactors = provide_all(Get{entity}Interactor)

     then add it to `create_container()` in setup/ioc/container.py.

  2. presentation/http/routers/__init__.py — mount the router:

         versioned.include_router({area}_router, prefix="/{area}", tags=["{area}"])

  3. make schema — regenerate openapi.json, and read the diff. That diff is the
     change your consumers will see.

Then: make check
"""


def _shown(path: Path) -> str:
    """Repository-relative when it can be, absolute otherwise.

    `relative_to` raises rather than returning the absolute path, and the tests
    scaffold into a temp directory — which is exactly where a generator should
    be exercised, so the printing has to cope with it.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def write(path: Path, content: str, *, force: bool) -> bool:
    if path.exists() and not force:
        print(f"  skip   {_shown(path)} (exists, use --force)")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"  write  {_shown(path)}")
    return True


def scaffold(
    area: str, *, root: Path = PACKAGE, tests: Path | None = None, force: bool = False
) -> list[Path]:
    one = singular(area)
    tests_root = tests if tests is not None else ROOT / "tests"
    files = {
        root / "application" / "interactors" / area / "__init__.py": interactor_init(
            area
        ),
        root / "application" / "interactors" / area / f"get_{one}.py": interactor_module(
            area
        ),
        root / "presentation" / "http" / "schemas" / "v1" / f"{area}.py": schema_module(
            area
        ),
        root / "presentation" / "http" / "routers" / "v1" / f"{area}.py": router_module(
            area
        ),
        tests_root / "http" / f"test_{area}_api.py": test_module(area),
    }
    written = [
        path for path, content in files.items() if write(path, content, force=force)
    ]
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="plural area name, e.g. orders")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args()

    area = args.name.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", area):
        print(f"name must be a lowercase identifier, got {args.name!r}", file=sys.stderr)
        return 1

    print(f"\nScaffolding the {area} vertical:\n")
    scaffold(area, force=args.force)

    # Formatted immediately, so the first thing the author sees is code the
    # repository already accepts rather than a lint failure they did not cause.
    subprocess.run(
        ["uv", "run", "ruff", "format", "--quiet", str(PACKAGE), str(ROOT / "tests")],
        check=False,
    )
    print(CHECKLIST.format(area=area, entity=camel(singular(area))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
