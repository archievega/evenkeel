from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dishka import AsyncContainer
from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI

from appcore.application.ports import MetricsPort
from appcore.infrastructure.adapters.noop.metrics import NoopMetrics
from appcore.logging import get_logger, setup_logging
from appcore.presentation.http.errors import setup_error_handlers
from appcore.presentation.http.middleware import ObservabilityMiddleware
from appcore.presentation.http.routers import setup_routes
from appcore.setup.config import Settings, load_settings
from appcore.setup.ioc.container import create_container

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Startup and, more importantly, orderly shutdown.

    Closing the container disposes the engine and its pool. Skipping it leaves
    server-side connections lingering until the database times them out, which
    on a rolling deploy means every replica's worth of dead connections
    competing for ``max_connections`` with the replicas replacing them.
    """
    log.info("application_started")
    try:
        yield
    finally:
        await app.state.dishka_container.close()
        log.info("application_stopped")


def create_app(
    settings: Settings | None = None,
    metrics: MetricsPort | None = None,
    container: AsyncContainer | None = None,
) -> FastAPI:
    """Build the application.

    Every argument is injectable so a test can construct the real app -- real
    routes, real middleware, real error handlers -- over fake adapters, without
    touching the environment. Production passes none of them. A test seam this
    small is worth more than the alternative, which is testing a different
    application than the one that ships.
    """
    resolved_settings = settings or load_settings()
    resolved_metrics = metrics or NoopMetrics()

    setup_logging(
        level=resolved_settings.app.logging.level,
        json_logs=resolved_settings.app.logging.json_logs,
        pretty_console=(
            resolved_settings.app.debug
            and resolved_settings.app.logging.console_pretty_in_debug
        ),
        include_timestamp=resolved_settings.app.logging.include_timestamp,
    )

    is_production = not resolved_settings.app.debug
    app = FastAPI(
        title=resolved_settings.app.name,
        debug=resolved_settings.app.debug,
        lifespan=lifespan,
        # Interactive docs describe every endpoint and schema; that is a gift
        # to a developer and a map to an attacker. Off unless debug.
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )

    app.add_middleware(ObservabilityMiddleware, metrics=resolved_metrics)
    setup_routes(
        app,
        prefix=resolved_settings.app.api.prefix,
        wallets_prefix=resolved_settings.app.api.wallets,
    )
    setup_error_handlers(app)

    resolved_container = container or create_container(
        resolved_settings, resolved_metrics
    )
    setup_dishka(resolved_container, app)
    return app
