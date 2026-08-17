"""Everything an outbound HTTP call needs to be safe, on plain aiohttp.

In the order they apply: a bulkhead that refuses rather than queues, a
per-attempt timeout and an overall budget, retries only where they can succeed,
and a response size cap. Nothing raises — the caller gets a `JsonResponse` with
a `failure` to branch on.

There is no circuit breaker; it was built twice, measured, and removed. Both
that and the guard settings: docs/adr/0006-outbound-calls.md.
"""

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import aiohttp
import structlog

from evenkeel.application.ports import BulkheadPolicy, BulkheadPort, MetricsPort
from evenkeel.logging import get_logger

log = get_logger(__name__)

CORRELATION_HEADER = "X-Correlation-ID"


class Failure(StrEnum):
    NONE = "none"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER_ERROR = "server_error"
    CLIENT_ERROR = "client_error"
    OVERSIZED = "oversized"
    MALFORMED = "malformed"
    REDIRECTED = "redirected"
    BULKHEAD_FULL = "bulkhead_full"
    BUDGET_EXHAUSTED = "budget_exhausted"


# Retried because the same request may still succeed: nothing was processed, or
# the provider told us to come back. Everything else — including a 400 and a
# malformed 200 — is deterministic, and retrying it only multiplies the damage.
_RETRYABLE = frozenset({Failure.TIMEOUT, Failure.CONNECTION, Failure.SERVER_ERROR})


@dataclass(frozen=True, slots=True)
class TransportPolicy:
    """Every knob, with the tail latency it implies stated out loud.

    Worst case for one call is bounded by `budget_ms`, not by
    `max_attempts * total_timeout_ms` — the budget is checked before each retry
    and shortens the last attempt's timeout to whatever is left. Without it,
    three attempts at 800ms plus backoff is a 2.5s p100 that nobody signed off.
    """

    service: str
    total_timeout_ms: int = 800
    connect_timeout_ms: int = 200
    read_timeout_ms: int = 700
    budget_ms: int = 1_500
    max_attempts: int = 2
    backoff_base_ms: int = 50
    backoff_max_ms: int = 400
    max_response_bytes: int = 64 * 1024
    bulkhead_limit: int = 32
    bulkhead_wait_ms: int = 0
    bulkhead_lease_ttl_ms: int = 5_000


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """Connection-level settings, separate from per-call ones.

    These belong to the session and outlive any single request, which is why
    they are not on `TransportPolicy`: mixing "how many sockets may exist" with
    "how long may this attempt take" is how a timeout change accidentally
    becomes a pool change.
    """

    connection_limit: int = 64
    dns_cache_seconds: int = 30
    trust_env: bool = False
    backstop_timeout_ms: int = 5_000


@asynccontextmanager
async def open_session(
    policy: SessionPolicy, *, headers: dict[str, str]
) -> AsyncIterator[aiohttp.ClientSession]:
    """The one place an outbound session is built, and the one place it closes.

    A context manager rather than a constructor because a `ClientSession` that
    is garbage-collected without `close()` leaves its connector's sockets open
    and prints an unhelpful warning at interpreter exit. Handing back a
    closeable object and trusting every call site is how this codebase already
    leaked four Redis clients.

    Takes a policy rather than the settings object: infrastructure sits below
    `setup` in the layer contract, and turning configuration into policy is the
    composition root's job. A config import here would let any adapter reach for
    any setting, which is how a "small" change to `DatabaseConfig` ends up
    breaking an HTTP client.
    """
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            # The transport-level ceiling on sockets. Related to the bulkhead
            # but not a substitute: exceeding this limit makes a caller *wait*
            # for a free connection, which is precisely the unbounded queue the
            # bulkhead exists to refuse. Keep it above the bulkhead limit so the
            # bulkhead answers first, with a decision rather than a delay.
            limit=policy.connection_limit,
            limit_per_host=policy.connection_limit,
            ttl_dns_cache=policy.dns_cache_seconds,
        ),
        headers={"User-Agent": "evenkeel-outbound", **headers},
        # Every request passes its own timeout; this is the backstop for any
        # path that forgets. aiohttp's default is 5 minutes, which on a request
        # path is indistinguishable from no timeout at all.
        timeout=aiohttp.ClientTimeout(total=policy.backstop_timeout_ms / 1000),
        # Ignore HTTP_PROXY and friends unless asked. An environment variable
        # that silently reroutes outbound traffic through a third party is a
        # supply-chain problem, not a convenience.
        trust_env=policy.trust_env,
    )
    try:
        yield session
    finally:
        await session.close()


@dataclass(frozen=True, slots=True)
class JsonResponse:
    status: int | None
    body: dict[str, Any] | None
    failure: Failure

    @property
    def ok(self) -> bool:
        return self.failure is Failure.NONE and self.body is not None


class JsonHttpTransport:
    """One provider's worth of outbound calls over a shared session.

    The session is passed in rather than created here: connection reuse only
    happens if one session outlives the request, and a session per call is the
    single most common way an async service ends up with thousands of sockets
    in TIME_WAIT and a p99 dominated by TLS handshakes.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        bulkhead: BulkheadPort,
        metrics: MetricsPort,
        policy: TransportPolicy,
        base_url: str,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._session = session
        self._bulkhead = bulkhead
        self._metrics = metrics
        self._policy = policy
        self._base_url = base_url.rstrip("/")
        # Holds the API key. Never logged, never included in an error, never
        # echoed into a metric label.
        self._default_headers = dict(default_headers or {})

    async def post_json(
        self, path: str, payload: dict[str, Any], *, operation: str
    ) -> JsonResponse:
        started_at = time.perf_counter()
        response = await self._guarded_post(path, payload)
        self._metrics.observe_external_call(
            service=self._policy.service,
            operation=operation,
            outcome=self._outcome_label(response),
            duration_seconds=time.perf_counter() - started_at,
        )
        return response

    async def _guarded_post(self, path: str, payload: dict[str, Any]) -> JsonResponse:
        lease = self._bulkhead.acquire(
            BulkheadPolicy(
                name=f"outbound:{self._policy.service}",
                limit=self._policy.bulkhead_limit,
                lease_ttl_ms=self._policy.bulkhead_lease_ttl_ms,
                wait_timeout_ms=self._policy.bulkhead_wait_ms,
            )
        )
        async with lease:
            if not lease.acquired:
                # Immediate, because `wait_timeout_ms` is 0. Queueing for a slot
                # is the failure mode the bulkhead exists to prevent: a caller
                # who waits behind a caller who is timing out has simply moved
                # the queue one layer up.
                return JsonResponse(status=None, body=None, failure=Failure.BULKHEAD_FULL)
            # The lease is held across every attempt, not per attempt. The limit
            # is on how many calls are *in flight*, and a retry is still the
            # same call occupying the same slot.
            return await self._attempt_loop(path, payload)

    async def _attempt_loop(self, path: str, payload: dict[str, Any]) -> JsonResponse:
        deadline = time.monotonic() + self._policy.budget_ms / 1000
        url = f"{self._base_url}/{path.lstrip('/')}"
        result = JsonResponse(status=None, body=None, failure=Failure.CONNECTION)

        for attempt in range(1, self._policy.max_attempts + 1):
            remaining_ms = (deadline - time.monotonic()) * 1000
            if remaining_ms <= 0:
                return JsonResponse(
                    status=None, body=None, failure=Failure.BUDGET_EXHAUSTED
                )

            result, retry_after_ms = await self._single_attempt(
                url, payload, remaining_ms
            )

            if result.failure not in _RETRYABLE or attempt == self._policy.max_attempts:
                return result

            backoff_ms = self._backoff_ms(attempt, retry_after_ms)
            if (deadline - time.monotonic()) * 1000 <= backoff_ms:
                # Sleeping would consume the budget and leave nothing for the
                # attempt it exists to enable. Returning now is honest; sleeping
                # first and then reporting the same failure is just slower.
                return result
            log.warning(
                "outbound_retry",
                service=self._policy.service,
                attempt=attempt,
                failure=result.failure.value,
                backoff_ms=round(backoff_ms),
            )
            await asyncio.sleep(backoff_ms / 1000)

        return result

    async def _single_attempt(
        self, url: str, payload: dict[str, Any], remaining_ms: float
    ) -> tuple[JsonResponse, float | None]:
        timeout = aiohttp.ClientTimeout(
            # Whichever is smaller: this attempt may not outlive the budget.
            total=min(self._policy.total_timeout_ms, remaining_ms) / 1000,
            connect=self._policy.connect_timeout_ms / 1000,
            # `total` alone does not bound a response that dribbles bytes
            # forever, because every byte resets nothing and the transfer is
            # technically progressing. `sock_read` is the one that stops it.
            sock_read=self._policy.read_timeout_ms / 1000,
        )
        try:
            async with self._session.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=timeout,
                # A redirect is a request to send this payload — and its API
                # key — somewhere the configuration never named. `aiohttp`
                # follows them by default and across hosts, so a provider whose
                # endpoint can be made to answer `302` reroutes the call to
                # whoever the `Location` names, and the answer that comes back
                # is trusted as theirs. This codebase already refuses proxy
                # environment variables for exactly that reason; following a
                # redirect is the same handover with extra steps.
                allow_redirects=False,
            ) as response:
                raw = await read_capped(
                    response.content, self._policy.max_response_bytes + 1
                )
                retry_after_ms = _retry_after_ms(response.headers.get("Retry-After"))

                # Status before size. The other way round, a 5xx or 429 whose
                # body carried a large error page was reported `OVERSIZED` and
                # lost both its retry and its `Retry-After`; and whoever
                # controlled a redirecting endpoint could pad the body to hide
                # the handover from the `REDIRECTED` signal that exists to show
                # it. `read_capped` made this reachable — a single buffered read
                # rarely returned more than one segment.
                if 300 <= response.status < 400:
                    return (
                        JsonResponse(response.status, None, Failure.REDIRECTED),
                        retry_after_ms,
                    )
                if response.status >= 500 or response.status == 429:
                    return (
                        JsonResponse(response.status, None, Failure.SERVER_ERROR),
                        retry_after_ms,
                    )
                if response.status >= 400:
                    return (
                        JsonResponse(response.status, None, Failure.CLIENT_ERROR),
                        retry_after_ms,
                    )
                if len(raw) > self._policy.max_response_bytes:
                    return (
                        JsonResponse(response.status, None, Failure.OVERSIZED),
                        retry_after_ms,
                    )
                body = decode_object(raw)
                if body is None:
                    return (
                        JsonResponse(response.status, None, Failure.MALFORMED),
                        retry_after_ms,
                    )
                return JsonResponse(response.status, body, Failure.NONE), retry_after_ms
        except TimeoutError:
            return JsonResponse(None, None, Failure.TIMEOUT), None
        except aiohttp.ClientError as exc:
            # Deliberately narrow: `aiohttp.ClientError` covers connection,
            # DNS, TLS and protocol errors. Catching `Exception` here would also
            # swallow `CancelledError` — the client hung up, and we would answer
            # a request nobody is waiting for — and every bug in this file.
            log.warning(
                "outbound_connection_error",
                service=self._policy.service,
                # The class, not the message: messages from a client library
                # carry hosts, ports and occasionally the URL with its query.
                error_type=type(exc).__name__,
            )
            return JsonResponse(None, None, Failure.CONNECTION), None

    def _headers(self) -> dict[str, str]:
        headers = dict(self._default_headers)
        correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
        if isinstance(correlation_id, str) and correlation_id:
            # The same id this service logs under. When the provider's support
            # asks "which request", there is one answer that works in both
            # systems' logs.
            headers[CORRELATION_HEADER] = correlation_id
        return headers

    def _backoff_ms(self, attempt: int, retry_after_ms: float | None) -> float:
        if retry_after_ms is not None:
            return min(retry_after_ms, self._policy.backoff_max_ms)
        ceiling = min(
            self._policy.backoff_max_ms, self._policy.backoff_base_ms * 2 ** (attempt - 1)
        )
        # Full jitter. Exponential backoff without it re-synchronises every
        # caller that failed at the same moment into a second simultaneous
        # wave, which is how a provider that was recovering goes back down.
        return random.uniform(0, ceiling)  # nosec B311 — jitter, not a secret

    @staticmethod
    def _outcome_label(response: JsonResponse) -> str:
        if response.failure is Failure.NONE:
            return "success"
        return response.failure.value


async def read_capped(stream: aiohttp.StreamReader, limit: int) -> bytes:
    """Read up to `limit` bytes, however many segments they arrive in.

    Two things at once, and the second is why this is not `response.json()`:

    A provider that answers with a gigabyte, by accident or otherwise, must not
    be able to spend this process's memory — so the read stops at the cap and
    the caller decides what an oversized body means.

    And `StreamReader.read(n)` returns what is *buffered*, not `n` bytes. A
    single call therefore truncates any response larger than one TCP segment,
    which on loopback never happens and against a real provider happens at
    about 1400 bytes. That bug shipped here: a 3 KB risk decision came back
    `MALFORMED`, and a `refuse` was reported to the caller as "provider
    unavailable".
    """
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(limit - size):
        chunks.append(chunk)
        size += len(chunk)
        if size >= limit:
            break
    return b"".join(chunks)


def decode_object(raw: bytes) -> dict[str, Any] | None:
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _retry_after_ms(header: str | None) -> float | None:
    """Seconds only. The HTTP-date form is legal and is ignored on purpose.

    Parsing a date means trusting a remote clock against ours, and the failure
    mode of getting it wrong — a negative delay read as "retry immediately", or
    an absolute time years away — is worse than falling back to our own backoff,
    which is always a sane number.
    """
    if not header:
        return None
    try:
        seconds = float(header)
    except ValueError:
        return None
    return max(0.0, seconds) * 1000
