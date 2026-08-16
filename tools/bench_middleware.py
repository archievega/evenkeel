"""Measure what the middleware costs, including the one that ships.

Run: `uv run python tools/bench_middleware.py`

Four rows, and the fourth is the one that matters. The first three isolate the
*base class* — a toy that appends a header, written twice, once on
`BaseHTTPMiddleware` and once as pure ASGI. The fourth runs the real
`ObservabilityMiddleware`: uuid, contextvars, route-pattern matching, metrics
and a log line per request.

The first version of this file stopped at the toy and the docstring in
`middleware.py` quoted its number as if it described the shipped class. It did
not, by a factor of ten. A benchmark that measures a stand-in is the same
failure as a claim with no benchmark at all, with more ceremony.

The endpoint is trivial, so these are an upper bound on relative cost — a
handler that touches a database dwarfs it. That is the point: this is the floor
paid on every request regardless of what the handler does.
"""

import asyncio
import os
import statistics
import time

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from evenkeel.infrastructure.adapters.noop.metrics import NoopMetrics
from evenkeel.logging import setup_logging
from evenkeel.presentation.http.middleware import ObservabilityMiddleware


class BaseHTTPVersion(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Trace"] = "x"
        return response


class PureASGIVersion:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-trace", b"x"))
            await send(message)

        await self.app(scope, receive, send_wrapper)


def build(mw):
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "alive"}

    if mw is ObservabilityMiddleware:
        # The shipped class needs its port. `NoopMetrics` keeps the call sites
        # real while measuring none of Prometheus's own cost.
        app.add_middleware(mw, metrics=NoopMetrics())
    elif mw:
        app.add_middleware(mw)
    return app


async def measure(app, n=2000):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.get("/health")
        start = time.perf_counter()
        for _ in range(n):
            await c.get("/health")
        return n / (time.perf_counter() - start)


async def main():
    results = {}
    for name, mw in (
        ("none", None),
        ("BaseHTTPMiddleware (toy)", BaseHTTPVersion),
        ("pure ASGI (toy)", PureASGIVersion),
        ("ObservabilityMiddleware", ObservabilityMiddleware),
    ):
        runs = [await measure(build(mw)) for _ in range(3)]
        results[name] = statistics.median(runs)
    base = results["none"]
    for name, rps in results.items():
        print(
            f"{name:26} {rps:8.0f} rps   "
            f"overhead vs none: {(base - rps) / base * 100:5.1f}%"
        )


if __name__ == "__main__":
    # Guarded, because importing this module used to run the whole benchmark —
    # found while importing `measure()` to answer a follow-up question, which is
    # the only way anyone would ever notice.
    #
    # The shipped middleware writes a log line per request, and that cost is real
    # in production. Sent to /dev/null rather than suppressed, so the formatting
    # is paid for and the terminal stays readable.
    with open(os.devnull, "w") as sink:
        setup_logging(level="INFO", json_logs=True, stream=sink)
        asyncio.run(main())
