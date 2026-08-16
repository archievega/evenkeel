from fastapi import APIRouter, FastAPI

from appcore.presentation.http.routers.health import router as health_router
from appcore.presentation.http.routers.v1.wallets import router as wallets_router


def setup_routes(app: FastAPI, *, prefix: str, wallets_prefix: str) -> None:
    """Mount versioned routers under a caller-supplied prefix.

    Prefixes arrive as plain strings rather than as the settings object:
    presentation should not import the composition root, or the dependency
    arrow quietly reverses and the layer check stops meaning anything.

    Operational endpoints stay unversioned at the root. Probes are consumed by
    the orchestrator, not by API clients, and moving them on a version bump
    silently breaks every deployment manifest.
    """
    app.include_router(health_router)

    versioned = APIRouter(prefix=prefix)
    versioned.include_router(wallets_router, prefix=wallets_prefix, tags=["wallets"])
    app.include_router(versioned)
