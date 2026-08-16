"""Measure what a middleware base class costs.

Run: `uv run python tools/bench_middleware.py`

This exists because the repo makes a claim in `presentation/http/middleware.py`
about why it is written as pure ASGI. A claim about performance that nobody can
reproduce is marketing; this is the receipt.

The endpoint is deliberately trivial, so the numbers are an upper bound on the
relative cost — a handler that touches a database dwarfs it. That is the point:
this is the floor of overhead you pay on every request regardless of what the
handler does.
"""

import asyncio
import statistics
import time

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send


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

    if mw:
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
        ("BaseHTTPMiddleware", BaseHTTPVersion),
        ("pure ASGI", PureASGIVersion),
    ):
        runs = [await measure(build(mw)) for _ in range(3)]
        results[name] = statistics.median(runs)
    base = results["none"]
    for name, rps in results.items():
        print(
            f"{name:22} {rps:8.0f} rps   overhead vs none: {(base - rps) / base * 100:5.1f}%"
        )


asyncio.run(main())
