from fastapi import APIRouter, FastAPI

from evenkeel.logging import get_logger
from evenkeel.presentation.http.routers.health import router as health_router
from evenkeel.presentation.http.routers.v1.wallets import router as wallets_router

log = get_logger(__name__)


def setup_routes(
    app: FastAPI, *, prefix: str, wallets_prefix: str, include_docs: bool = False
) -> None:
    """Mount versioned routers under a caller-supplied prefix.

    Prefixes arrive as plain strings rather than as the settings object:
    presentation should not import the composition root, or the dependency
    arrow quietly reverses and the layer check stops meaning anything.

    Operational endpoints stay unversioned at the root. Probes are consumed by
    the orchestrator, not by API clients, and moving them on a version bump
    silently breaks every deployment manifest.
    """
    app.include_router(health_router)
    if include_docs:
        # Mounted on the same condition as /docs and /redoc, not separately —
        # one decision about the documentation surface, taken in one place.
        #
        # The renderer lives behind the `docs` extra, so an install without it
        # must lose the page rather than refuse to boot: no optional dependency
        # in this template is allowed to be load-bearing. Swagger and ReDoc are
        # built into FastAPI and still there. The warning exists because a
        # missing page with no explanation is its own kind of bug report.
        try:
            from evenkeel.presentation.http.routers.docs import router as docs_router
        except ImportError:
            log.warning("scalar_unavailable", extra_required="docs")
        else:
            app.include_router(docs_router)

    versioned = APIRouter(prefix=prefix)
    versioned.include_router(wallets_router, prefix=wallets_prefix, tags=["wallets"])
    app.include_router(versioned)
