import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from evenkeel.application.ports import MetricsPort
from evenkeel.logging import get_logger

log = get_logger(__name__)

CORRELATION_HEADER = "X-Correlation-ID"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Correlation id, request log and request metrics in one pass.

    The id is taken from the inbound header when present so a trace survives
    across services, and echoed back so a user can quote it in a bug report.
    """

    def __init__(self, app: ASGIApp, metrics: MetricsPort) -> None:
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            http_method=request.method,
            http_path=request.url.path,
        )

        self._metrics.request_started()
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            self._metrics.request_failed(
                method=request.method,
                handler=_handler_label(request),
                exception_type=type(exc).__name__,
                duration_seconds=time.perf_counter() - started_at,
            )
            raise
        else:
            duration = time.perf_counter() - started_at
            self._metrics.request_finished(
                method=request.method,
                handler=_handler_label(request),
                status_code=response.status_code,
                duration_seconds=duration,
            )
            response.headers[CORRELATION_HEADER] = correlation_id
            log.info(
                "http_request",
                status_code=response.status_code,
                duration_seconds=round(duration, 4),
            )
            return response
        finally:
            structlog.contextvars.clear_contextvars()


def _handler_label(request: Request) -> str:
    """Use the route template, never the raw path.

    ``/v1/wallets/{wallet_id}`` is one time series; the raw path is one series
    per wallet, which is how a metrics backend runs out of memory.
    """
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")
