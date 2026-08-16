from evenkeel.application.ports.risk import (
    RiskAssessmentPort,
    RiskCheck,
    RiskDecision,
    RiskOutcome,
)


class AllowAllRiskAssessment(RiskAssessmentPort):
    """The default, and the reason `docker compose up` needs no third party.

    A template whose money path requires a vendor account is a template nobody
    can run. This adapter keeps the call site, the metrics and the decision
    branches real while answering `ALLOWED` in constant time, so the HTTP
    adapter is a configuration change rather than a rewrite.

    It is deliberately not called `NoopRiskAssessment`. It is not a no-op: it
    takes a position — everything is allowed — and that position is a choice
    someone should have to make on purpose before this reaches production. The
    boot guard says so out loud.
    """

    async def assess(self, check: RiskCheck) -> RiskDecision:
        return RiskDecision(outcome=RiskOutcome.ALLOWED, reason="no provider configured")
