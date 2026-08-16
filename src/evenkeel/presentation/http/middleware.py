import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from evenkeel.application.ports import MetricsPort
from evenkeel.logging import get_logger

log = get_logger(__name__)

CORRELATION_HEADER = "X-Correlation-ID"
_CORRELATION_HEADER_BYTES = CORRELATION_HEADER.lower().encode()
CORRELATION_SCOPE_KEY = "evenkeel.correlation_id"


class ObservabilityMiddleware:
    """Correlation id, request log and request metrics in one pass.

    Written as pure ASGI rather than `BaseHTTPMiddleware`, and the difference is
    not academic. `BaseHTTPMiddleware` runs the downstream app in a task pair
    joined by an anyio memory stream so it can hand you `Request`/`Response`
    objects; across runs that machinery cost **55-65% of throughput** on a
    trivial endpoint, against **1-3%** for the version below. Reproduce with
    `tools/bench_middleware.py` rather than taking the number on faith.

    The relative cost shrinks once a handler does real I/O, but it is paid on
    every request by every endpoint forever, and avoiding it costs only the mild
    inconvenience of handling raw ASGI messages.

    The id is taken from the inbound header when present so a trace survives
    across services, and echoed back so a user can quote it in a bug report.
    """

    def __init__(self, app: ASGIApp, metrics: MetricsPort) -> None:
        self._app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan and websocket traffic is none of this middleware's business.
        # Treating an unknown scope type as HTTP is how ASGI middleware breaks on
        # the first protocol it did not anticipate.
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        correlation_id = _inbound_correlation_id(scope) or str(uuid.uuid4())
        # Also on the scope, not only in contextvars. Starlette installs the
        # catch-all `Exception` handler on ServerErrorMiddleware, which is the
        # OUTERMOST layer — so an unhandled exception is rendered after this
        # middleware's `finally` has already cleared the context. Anything that
        # reads the id from contextvars gets nothing on exactly the responses
        # where the id matters most. The scope belongs to the request and no
        # other layer can wipe it.
        scope[CORRELATION_SCOPE_KEY] = correlation_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            http_method=scope.get("method", ""),
            http_path=scope.get("path", ""),
        )

        self._metrics.request_started()
        started_at = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((_CORRELATION_HEADER_BYTES, correlation_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except Exception as exc:
            self._metrics.request_failed(
                method=scope.get("method", ""),
                handler=_handler_label(scope),
                exception_type=type(exc).__name__,
                duration_seconds=time.perf_counter() - started_at,
            )
            raise
        else:
            duration = time.perf_counter() - started_at
            self._metrics.request_finished(
                method=scope.get("method", ""),
                handler=_handler_label(scope),
                status_code=status_code,
                duration_seconds=duration,
            )
            log.info(
                "http_request",
                status_code=status_code,
                duration_seconds=round(duration, 4),
            )
        finally:
            structlog.contextvars.clear_contextvars()


def _inbound_correlation_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == _CORRELATION_HEADER_BYTES:
            decoded: str = value.decode("latin-1")
            return decoded
    return None


def _handler_label(scope: Scope) -> str:
    """Use the route template, never the raw path.

    `/v1/wallets/{wallet_id}` is one time series; the raw path is one series per
    wallet, which is how a metrics backend runs out of memory. The router writes
    the matched route into the scope while handling, so this is read after the
    downstream app has run rather than before.
    """
    route = scope.get("route")
    return getattr(route, "path", "unmatched")
