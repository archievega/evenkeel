import re
import time
import uuid

import structlog
from starlette.routing import compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from evenkeel.application.ports import MetricsPort
from evenkeel.logging import get_logger

log = get_logger(__name__)

CORRELATION_HEADER = "X-Correlation-ID"
_CORRELATION_HEADER_BYTES = CORRELATION_HEADER.lower().encode()
CORRELATION_SCOPE_KEY = "evenkeel.correlation_id"


class ObservabilityMiddleware:
    """Correlation id, request log and request metrics in one pass.

    Written as pure ASGI rather than `BaseHTTPMiddleware`, which runs the
    downstream app in a task pair joined by an anyio memory stream so it can
    hand you `Request`/`Response` objects. On a trivial endpoint, for identical
    work: **56% of throughput against 4%**.

    This class then costs **~28%** of its own, because of what it does — a uuid,
    contextvars, route-pattern matching, metrics, and a log line, of which the
    log line is about 5 points. That is the number to quote, and the earlier
    version of this docstring quoted the 4% instead, which described a stand-in
    the benchmark measured rather than the class that ships.

    `tools/bench_middleware.py` produces all four rows. The endpoint is trivial,
    so these are an upper bound: a handler that touches a database dwarfs them.

    The id is taken from the inbound header when present so a trace survives
    across services, and echoed back so a user can quote it in a bug report.
    """

    def __init__(self, app: ASGIApp, metrics: MetricsPort) -> None:
        self._app = app
        self._metrics = metrics
        # Compiled full-path templates, built once on the first request.
        # Routes are registered before traffic arrives but after this object
        # exists, so this cannot be done in __init__.
        self._patterns: list[tuple[re.Pattern[str], str]] | None = None

    def _handler_label(self, scope: Scope) -> str:
        """The route template, never the raw path.

        `/v1/wallets/{wallet_id}` is one time series; the raw path is one series
        per wallet, which is how a metrics backend runs out of memory. Anything
        matching no route collapses to a single `unmatched`, so scanner traffic
        cannot mint a series per probed URL either.
        """
        if self._patterns is None:
            self._patterns = _build_route_patterns(scope.get("app"))
        path = str(scope.get("path", ""))
        for pattern, template in self._patterns:
            if pattern.fullmatch(path):
                return template
        return "unmatched"

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
                # Only if absent. Error responses are rendered by `problem()`,
                # which sets the header itself because the catch-all handler
                # runs outside this wrapper — appending unconditionally gave
                # those responses the header twice, which a client reads as the
                # single comma-joined value "id, id".
                if not any(name == _CORRELATION_HEADER_BYTES for name, _ in headers):
                    headers.append(
                        (_CORRELATION_HEADER_BYTES, correlation_id.encode("latin-1"))
                    )
                message["headers"] = headers
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except Exception as exc:
            self._metrics.request_failed(
                method=scope.get("method", ""),
                handler=self._handler_label(scope),
                exception_type=type(exc).__name__,
                duration_seconds=time.perf_counter() - started_at,
            )
            raise
        else:
            duration = time.perf_counter() - started_at
            self._metrics.request_finished(
                method=scope.get("method", ""),
                handler=self._handler_label(scope),
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


def _build_route_patterns(
    router: object, prefix: str = ""
) -> list[tuple[re.Pattern[str], str]]:
    """Compile every FULL path template once, to match raw paths against later.

    Two things make the obvious approaches wrong. `scope["route"].path` is
    relative to the router that owns it, so `GET /v1/wallets` reports `""` and
    `/v1/wallets/{wallet_id}` reports `/{wallet_id}` — distinct endpoints
    collapse into one label and collide with any other router owning an
    `{id}`-shaped route. And matching on route identity fails too: FastAPI's
    nested routers put the original route object on the scope while `app.routes`
    exposes wrappers, so the two never compare equal.

    Accumulating prefixes and recompiling gives the real template. The cost is
    a handful of regex matches against a short string, once per request.
    """
    patterns: list[tuple[re.Pattern[str], str]] = []
    for route in getattr(router, "routes", []):
        # An included router is a wrapper holding the router it was built from
        # plus the prefix it was mounted at. These two attribute names are
        # FastAPI internals, and the tests in
        # `tests/http/test_observability_middleware.py` exist so that a version
        # bump renaming them fails loudly instead of silently degrading every
        # metric label back to a relative path.
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            patterns.extend(
                _build_route_patterns(
                    included, prefix + str(getattr(context, "prefix", ""))
                )
            )
            continue

        nested = getattr(route, "routes", None)
        if nested:
            patterns.extend(
                _build_route_patterns(route, prefix + str(getattr(route, "path", "")))
            )
            continue

        template = prefix + str(getattr(route, "path", "")) or "/"
        patterns.append((compile_path(template)[0], template))
    return patterns


def _inbound_correlation_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == _CORRELATION_HEADER_BYTES:
            decoded: str = value.decode("latin-1")
            return decoded
    return None


def _build_label_map(router: object, prefix: str = "") -> dict[int, str]:
    """Map every route object to its FULL path template.

    `scope["route"].path` alone is not it. FastAPI nests included routers, and
    the route object it puts on the scope carries only its own leaf path — so
    `GET /v1/wallets` reports `""` and `GET /v1/wallets/{wallet_id}` reports
    `/{wallet_id}`. Two different endpoints then share one empty label, and any
    second router with an `{id}`-shaped route collides with this one. Walking
    the tree once and accumulating prefixes gives the template the docstring
    below promises.
    """
    labels: dict[int, str] = {}
    for route in getattr(router, "routes", []):
        path = prefix + str(getattr(route, "path", ""))
        nested = getattr(route, "routes", None)
        if nested:
            labels.update(_build_label_map(route, path))
        else:
            labels[id(route)] = path or "/"
    return labels
