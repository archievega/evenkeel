"""A third rendering of the same schema, mounted only on a local run.

FastAPI ships Swagger UI and ReDoc. Scalar is added beside them because it is
the one with a working request client, a search that covers the whole surface,
and a layout that reads like documentation rather than a debugging console — and
because swapping a renderer is a two-line change when the schema is the artefact
and the UI is not.

Nothing here is available outside `local`: these pages describe every endpoint,
field and error shape, which is a gift to a developer and a map to anyone else.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from scalar_fastapi import get_scalar_api_reference

router = APIRouter(include_in_schema=False)


@router.get("/scalar")
async def scalar_reference() -> HTMLResponse:
    return get_scalar_api_reference(
        openapi_url="/openapi.json",
        title="evenkeel API",
        # Off by default in the library. It reports whether a request was sent
        # through the built-in client — harmless in intent, but a template
        # should not opt its users into an outbound call they did not ask for
        # and would have to discover by reading the dependency.
        telemetry=False,
        dark_mode=True,
        # The renderer is fetched from a CDN. Acceptable for a page that only
        # runs locally; if these docs are ever exposed, vendor the asset instead
        # so the page does not depend on a third party being up and honest.
        scalar_js_url="https://cdn.jsdelivr.net/npm/@scalar/api-reference",
    )
