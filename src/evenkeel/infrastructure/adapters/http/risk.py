from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp

from evenkeel.application.ports import BulkheadPort, MetricsPort
from evenkeel.application.ports.risk import (
    RiskAssessmentPort,
    RiskCheck,
    RiskDecision,
    RiskOutcome,
)
from evenkeel.infrastructure.adapters.http.transport import (
    JsonHttpTransport,
    SessionPolicy,
    TransportPolicy,
)
from evenkeel.logging import get_logger

log = get_logger(__name__)

_ALLOW = "allow"
_REFUSE = "refuse"


class HttpRiskAssessment(RiskAssessmentPort):
    """Maps one provider's JSON onto the port's three outcomes.

    Thin on purpose. Everything that makes the call survivable lives in the
    transport; what is left here is the part that is specific to this provider
    and would have to be rewritten for a different one — the request shape, the
    response shape, and the mapping.

    The request contract, which the stub in `tools/load/risk_provider.py`
    implements:

        POST {base_url}{path}
        {"wallet_id", "owner_id", "amount", "currency", "direction",
         "idempotency_key"}
        -> 200 {"decision": "allow"|"refuse", "reason": str, "reference": str}
    """

    def __init__(self, transport: JsonHttpTransport, *, path: str) -> None:
        self._transport = transport
        self._path = path

    async def assess(self, check: RiskCheck) -> RiskDecision:
        response = await self._transport.post_json(
            self._path,
            {
                "wallet_id": str(check.wallet_id.value),
                "owner_id": str(check.owner_id.value),
                # A string, like everywhere else money crosses a boundary in
                # this codebase. JSON numbers are IEEE 754 doubles on the other
                # side of almost every parser, and 0.1 + 0.2 is not 0.3.
                "amount": str(check.amount.amount),
                "currency": check.amount.currency.value,
                "direction": check.direction.value,
                "idempotency_key": check.idempotency_key,
            },
            operation="assess",
        )
        if not response.ok or response.body is None:
            return RiskDecision(
                outcome=RiskOutcome.UNAVAILABLE, reason=response.failure.value
            )

        decision = response.body.get("decision")
        if decision == _ALLOW:
            return RiskDecision(
                outcome=RiskOutcome.ALLOWED,
                reason=_text(response.body.get("reason")),
                reference=_text(response.body.get("reference")),
            )
        if decision == _REFUSE:
            return RiskDecision(
                outcome=RiskOutcome.REFUSED,
                reason=_text(response.body.get("reason")),
                reference=_text(response.body.get("reference")),
            )
        # A 200 this code cannot read is not permission. Defaulting to ALLOWED
        # here would mean a provider that renamed a field silently disables
        # every check, and nothing would look broken until an audit.
        log.warning("risk_decision_unrecognised", service="risk")
        return RiskDecision(outcome=RiskOutcome.UNAVAILABLE, reason="unrecognised")


def _text(value: object) -> str:
    """Provider prose, truncated. It reaches our logs, and it is not our text."""
    return str(value)[:200] if isinstance(value, str) else ""


@asynccontextmanager
async def open_http_risk_assessment(
    *,
    base_url: str,
    path: str,
    api_key: str,
    transport_policy: TransportPolicy,
    session_policy: SessionPolicy,
    bulkhead: BulkheadPort,
    metrics: MetricsPort,
) -> AsyncIterator[RiskAssessmentPort]:
    """Owns the session, so the caller cannot forget to close it.

    A context manager rather than a constructor because an `aiohttp`
    `ClientSession` that is garbage-collected without `close()` leaves its
    connector's sockets open and prints an unhelpful warning at interpreter
    exit. Handing back a closeable object and trusting every call site is how
    this codebase already leaked four Redis clients.

    Takes policies rather than the settings object: infrastructure sits below
    `setup` in the layer contract, and translating configuration into policy is
    the composition root's job. The linter enforces it, and the reason it is
    worth enforcing is that a config import here would let any adapter reach for
    any setting, which is how a "small" change to `DatabaseConfig` ends up
    breaking an HTTP client.
    """
    connector = aiohttp.TCPConnector(
        # The transport-level ceiling on sockets. Related to the bulkhead but
        # not a substitute: exceeding this limit makes a caller *wait* for a
        # free connection, which is precisely the unbounded queue the bulkhead
        # exists to refuse. Keep it above the bulkhead limit so the bulkhead is
        # the one that answers first, with a decision rather than a delay.
        limit=session_policy.connection_limit,
        limit_per_host=session_policy.connection_limit,
        ttl_dns_cache=session_policy.dns_cache_seconds,
    )
    headers = {"User-Agent": "evenkeel-outbound"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    session = aiohttp.ClientSession(
        connector=connector,
        headers=headers,
        # Every request passes its own timeout; this is the backstop for any
        # path that forgets. aiohttp's default is 5 minutes, which for a call
        # on a request path is indistinguishable from no timeout at all.
        timeout=aiohttp.ClientTimeout(total=session_policy.backstop_timeout_ms / 1000),
        # Ignore HTTP_PROXY and friends unless asked. An environment variable
        # that silently reroutes outbound traffic through a third party is a
        # supply-chain problem, not a convenience.
        trust_env=session_policy.trust_env,
    )
    try:
        yield HttpRiskAssessment(
            JsonHttpTransport(
                session,
                bulkhead=bulkhead,
                metrics=metrics,
                policy=transport_policy,
                base_url=base_url,
            ),
            path=path,
        )
    finally:
        await session.close()
        log.info("outbound_session_closed", service=transport_policy.service)
