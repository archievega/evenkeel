from evenkeel.application.ports.risk import (
    RiskAssessmentPort,
    RiskCheck,
    RiskDecision,
    RiskOutcome,
)


class AllowAllRiskAssessment(RiskAssessmentPort):
    """The default, and the reason `docker compose up` needs no third party.

    Keeps the call site, the metrics and the decision branches real while
    answering `ALLOWED` in constant time, so the HTTP adapter is a
    configuration change rather than a rewrite.

    Not named `Noop`: it takes a position — everything is allowed — and outside
    `local` the provider in `setup/ioc` warns about it at boot.
    """

    async def assess(self, check: RiskCheck) -> RiskDecision:
        return RiskDecision(outcome=RiskOutcome.ALLOWED, reason="no provider configured")
