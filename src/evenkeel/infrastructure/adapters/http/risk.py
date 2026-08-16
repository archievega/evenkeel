from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
    open_session,
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
    """Maps configuration onto one provider, over a session it owns.

    The session, its limits and its lifetime are `open_session`'s; what is here
    is the provider-specific part — the API key and the transport policy.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with open_session(session_policy, headers=headers) as session:
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
