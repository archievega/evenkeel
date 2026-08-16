import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dishka import AsyncContainer
from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI

from evenkeel.application.ports import MetricsPort
from evenkeel.infrastructure.adapters.noop.metrics import NoopMetrics
from evenkeel.logging import get_logger, setup_logging
from evenkeel.presentation.http.errors import setup_error_handlers
from evenkeel.presentation.http.middleware import ObservabilityMiddleware
from evenkeel.presentation.http.routers import setup_routes
from evenkeel.setup.config import Settings, load_settings
from evenkeel.setup.ioc.container import create_container

log = get_logger(__name__)

API_DESCRIPTION = """
A wallet ledger, used here as the reference vertical for a backend template.

**Reading the error contract.** Every failure is an
[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) `application/problem+json`
document. Branch on `code`, never on `title` — titles are prose and may be
reworded, codes are part of the contract. Every response carries a
`correlation_id`, in the body and in the `X-Correlation-ID` header; quote it
when reporting a failure and it will find the server-side log line.

**Retrying safely.** Send an `Idempotency-Key` header on deposits and
withdrawals. A retry then returns the original entry with `replayed: true`
instead of moving money again — including when the retry arrives while the
first request is still in flight. Reusing one key with a different payload is
refused with `409`, never silently confirmed.

**Ownership.** Every read is filtered by owner in SQL rather than checked
afterwards, so a wallet belonging to someone else is reported as absent rather
than forbidden. The status code cannot be used to discover which ids exist.

**Money.** Amounts are decimal strings, never JSON numbers, and arithmetic
across currencies is refused rather than converted.
"""

TAGS_METADATA = [
    {
        "name": "wallets",
        "description": (
            "Open a wallet, move money, read the ledger. Balance changes are "
            "guarded by an idempotency key, a per-wallet lock and an optimistic "
            "version check, each covering what the others cannot."
        ),
    },
    {
        "name": "operations",
        "description": (
            "Probes for the orchestrator, unversioned because deployment "
            "manifests reference them. `/health` deliberately checks nothing "
            "external: a liveness probe that fails during a database blip "
            "restarts every healthy replica and turns a partial outage into a "
            "total one. `/ready` is the one that checks dependencies."
        ),
    },
]


def _build_metrics(settings: Settings) -> MetricsPort:
    """No-op unless asked, and asked for by configuration rather than by import.

    The instrumentation calls are unconditional in the code above; what changes
    is which object receives them. That is the whole null-adapter argument in
    one function — no `if metrics_enabled` scattered through the use cases.
    """
    if not settings.observability.metrics_enabled:
        return NoopMetrics()

    from evenkeel.infrastructure.adapters.prometheus.metrics import PrometheusMetrics

    return PrometheusMetrics()


def _mount_metrics_endpoint(
    app: FastAPI, metrics: MetricsPort, settings: Settings
) -> None:
    """`/metrics`, and only when there is something to expose.

    Not authenticated. It publishes route templates, outcome counts and
    latencies — no request bodies and no ids, by construction — but it is still
    an internal surface, and the deployment is expected to keep it off the
    public listener. Said here rather than assumed, because the alternative is
    discovering it in someone else's scan.
    """
    registry = getattr(metrics, "registry", None)
    if registry is None or not settings.observability.metrics_enabled:
        return

    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    from starlette.requests import Request
    from starlette.responses import Response

    async def scrape(request: Request) -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    # A route, not `app.mount("/metrics", make_asgi_app(...))`. A mount answers
    # the bare path with a 307 to `/metrics/`, which every scraper then follows
    # on every scrape forever — working, and paying a round trip to do it.
    app.add_route("/metrics", scrape, methods=["GET"], include_in_schema=False)


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
    resolved_metrics = metrics or _build_metrics(resolved_settings)

    setup_logging(
        level=resolved_settings.app.logging.level,
        json_logs=resolved_settings.app.logging.json_logs,
        pretty_console=(
            resolved_settings.app.is_local
            and resolved_settings.app.logging.console_pretty_in_debug
        ),
        include_timestamp=resolved_settings.app.logging.include_timestamp,
    )

    is_production = not resolved_settings.app.is_local
    app = FastAPI(
        title=resolved_settings.app.name,
        description=API_DESCRIPTION,
        version=os.getenv("APP_VERSION", "dev"),
        openapi_tags=TAGS_METADATA,
        # Declared so a "try it" client in any renderer knows where to send.
        # Relative, because the template does not know its own public host.
        servers=[{"url": "/", "description": "This deployment"}],
        # `debug` is never passed through. Starlette's ServerErrorMiddleware
        # checks it BEFORE consulting the installed handler, so a truthy value
        # replaces the RFC 9457 document with an HTML page carrying the
        # exception message, the source frames and — for a driver error — the
        # DSN. The handler in `errors.py` must always be the thing that renders.
        lifespan=lifespan,
        # Interactive docs describe every endpoint and schema; that is a gift
        # to a developer and a map to an attacker. Off unless debug.
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )

    _mount_metrics_endpoint(app, resolved_metrics, resolved_settings)
    app.add_middleware(ObservabilityMiddleware, metrics=resolved_metrics)
    setup_routes(
        app,
        prefix=resolved_settings.app.api.prefix,
        wallets_prefix=resolved_settings.app.api.wallets,
        include_docs=not is_production,
    )
    setup_error_handlers(app)

    resolved_container = container or create_container(
        resolved_settings, resolved_metrics
    )
    setup_dishka(resolved_container, app)
    return app
