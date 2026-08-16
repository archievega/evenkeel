#!/usr/bin/env python3
"""A stand-in for the risk provider, with a dial for how badly it behaves.

Exists so the resilience code can be *demonstrated* rather than asserted. A
bulkhead that has never shed anything and a circuit breaker that has never
opened are decoration; pointing the service at this and turning `LATENCY_MS` up
is how you find out whether they do what their docstrings claim.

    LATENCY_MS=2000 python tools/load/risk_provider.py   # slow, never fails
    FAILURE_RATE=1.0 python tools/load/risk_provider.py  # down
    REFUSE_RATE=0.1 python tools/load/risk_provider.py   # refuses one in ten

Deliberately not a fixture and not importable by the application: it is a test
double that happens to be a process, and it lives under `tools/` so nobody can
mistake it for something that ships.
"""

import asyncio
import os

# `random` shapes test traffic here; nothing generated is a secret.
import random  # nosec B311

from aiohttp import web

LATENCY_MS = float(os.getenv("LATENCY_MS", "5"))
JITTER_MS = float(os.getenv("JITTER_MS", "0"))
FAILURE_RATE = float(os.getenv("FAILURE_RATE", "0"))
REFUSE_RATE = float(os.getenv("REFUSE_RATE", "0"))
PORT = int(os.getenv("PORT", "9800"))


async def decide(request: web.Request) -> web.StreamResponse:
    delay_ms = LATENCY_MS + random.uniform(0, JITTER_MS)  # nosec B311
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)

    roll = random.random()  # nosec B311
    if roll < FAILURE_RATE:
        return web.json_response({"error": "unavailable"}, status=503)
    if roll < FAILURE_RATE + REFUSE_RATE:
        return web.json_response(
            {"decision": "refuse", "reason": "sampled", "reference": "stub"}
        )
    return web.json_response({"decision": "allow", "reference": "stub"})


async def health(request: web.Request) -> web.StreamResponse:
    return web.json_response({"status": "ok"})


def main() -> None:
    app = web.Application()
    app.router.add_post("/decisions", decide)
    app.router.add_get("/health", health)
    print(
        f"risk stub on :{PORT} "
        f"latency={LATENCY_MS}ms jitter={JITTER_MS}ms "
        f"failure={FAILURE_RATE} refuse={REFUSE_RATE}",
        flush=True,
    )
    # Binds all interfaces: a container that binds loopback is unreachable from
    # the service calling it. This process only ever runs under the `load`
    # profile, never in a deployment.
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)  # nosec B104


if __name__ == "__main__":
    main()
