import asyncio
import os

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from appcore.presentation.http.schemas.health import (
    DependencyStatus,
    LivenessResponse,
    ReadinessResponse,
    VersionResponse,
)

router = APIRouter(route_class=DishkaRoute, tags=["operations"])

PROBE_TIMEOUT_SECONDS = 2.0


@router.get("/health", response_model=LivenessResponse)
async def liveness() -> LivenessResponse:
    """Liveness: is this process still able to serve?

    Deliberately checks nothing external. A liveness probe that fails when the
    database is briefly unreachable makes the orchestrator kill every healthy
    replica during a database blip, turning a partial outage into a total one.
    """
    return LivenessResponse(status="alive")


@router.get("/ready", response_model=ReadinessResponse)
async def readiness(
    engine: FromDishka[AsyncEngine], response: Response
) -> ReadinessResponse:
    """Readiness: can this process serve *right now*?

    Returning a hardcoded "ready" is worse than having no probe at all: the
    load balancer keeps routing to a replica that cannot reach its database,
    and the deploy that broke the connection string still goes green.
    """
    database = await _check_database(engine)
    ready = database.healthy
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if ready else "degraded",
        dependencies={"database": database},
    )


@router.get("/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    """What is actually deployed here.

    Injected at build time. Without it, "is the fix live?" is answered by
    guesswork during the incident where it matters most.
    """
    return VersionResponse(
        version=os.getenv("APP_VERSION", "dev"),
        commit=os.getenv("APP_COMMIT", "unknown"),
        built_at=os.getenv("APP_BUILT_AT", "unknown"),
    )


async def _check_database(engine: AsyncEngine) -> DependencyStatus:
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except TimeoutError:
        return DependencyStatus(healthy=False, error="timeout")
    except Exception as exc:
        # The probe reports the failure class, never the driver message: DSNs
        # and host names end up in that string.
        return DependencyStatus(healthy=False, error=type(exc).__name__)
    return DependencyStatus(healthy=True, error=None)
