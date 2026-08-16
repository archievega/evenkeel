import time
from dataclasses import dataclass

from evenkeel.application.errors import (
    ApplicationError,
    ApplicationErrorCode,
    DependencyUnavailableError,
    ForbiddenError,
    RateLimitedError,
)
from evenkeel.application.ports import (
    MetricsPort,
    RateLimiterPort,
    RateLimitPolicy,
)
from evenkeel.application.ports.risk import (
    RiskAssessmentPort,
    RiskCheck,
    RiskDecision,
    RiskOutcome,
)
from evenkeel.application.services.wallet_movement import (
    MovementRequest,
    WalletMovementService,
)
from evenkeel.domain.entities.ledger_entry import LedgerDirection
from evenkeel.domain.value_objects.ids import LedgerEntryId, OwnerId, WalletId
from evenkeel.domain.value_objects.money import Money


@dataclass(frozen=True, slots=True)
class MoveMoneySettings:
    rate_limit_enabled: bool = True
    rate_limit_limit: int = 30
    rate_limit_window_seconds: float = 60.0
    # What happens when the risk provider cannot be reached. False refuses the
    # movement with a 503; True lets it through and records that it was not
    # assessed. There is no correct answer in general — there is only a decision
    # someone has to have made on purpose, which is why it is configuration and
    # not a default buried in an adapter.
    risk_fail_open: bool = False


@dataclass(frozen=True, slots=True)
class DepositCommand:
    wallet_id: WalletId
    owner_id: OwnerId
    amount: Money
    description: str = ""
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class WithdrawCommand:
    wallet_id: WalletId
    owner_id: OwnerId
    amount: Money
    description: str = ""
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class MovementResult:
    entry_id: LedgerEntryId
    wallet_id: WalletId
    balance: Money
    replayed: bool


class _MovementInteractor:
    """Shared orchestration: rate limit, delegate, measure.

    The interactor owns cross-cutting policy and the service owns the write
    path, so neither grows into the 500-line procedure that use cases turn into
    when both concerns live together.
    """

    _metric_name: str
    _direction: LedgerDirection

    def __init__(
        self,
        movement: WalletMovementService,
        rate_limiter: RateLimiterPort,
        risk: RiskAssessmentPort,
        metrics: MetricsPort,
        settings: MoveMoneySettings,
    ) -> None:
        self._movement = movement
        self._rate_limiter = rate_limiter
        self._risk = risk
        self._metrics = metrics
        self._settings = settings

    async def _run(
        self,
        *,
        wallet_id: WalletId,
        owner_id: OwnerId,
        amount: Money,
        description: str,
        idempotency_key: str | None,
    ) -> MovementResult:
        started_at = time.perf_counter()
        outcome = "error"
        try:
            if self._settings.rate_limit_enabled:
                decision = await self._rate_limiter.consume(
                    key=f"{self._metric_name}:{owner_id.value}",
                    policy=RateLimitPolicy(
                        name=self._metric_name,
                        limit=self._settings.rate_limit_limit,
                        window_seconds=self._settings.rate_limit_window_seconds,
                    ),
                )
                self._metrics.observe_rate_limit(
                    policy=self._metric_name,
                    decision="allowed" if decision.allowed else "denied",
                )
                if not decision.allowed:
                    outcome = "rate_limited"
                    raise RateLimitedError(
                        ApplicationErrorCode.RATE_LIMITED,
                        details={"retry_after_seconds": decision.retry_after_seconds},
                    )

            # Before the lock and before the transaction, deliberately. The
            # alternative is holding a per-wallet lock and a database
            # connection across a call to someone else's service, which is how
            # one slow provider becomes an empty connection pool and an outage
            # on endpoints that never touch it.
            #
            # The cost is that a retried request is assessed again. That is why
            # the idempotency key is passed through: the provider gets the same
            # key we did and can deduplicate on its side, which is the only
            # place the deduplication can be authoritative.
            risk_decision = await self._risk.assess(
                RiskCheck(
                    wallet_id=wallet_id,
                    owner_id=owner_id,
                    amount=amount,
                    direction=self._direction,
                    idempotency_key=idempotency_key,
                )
            )
            if risk_decision.outcome is RiskOutcome.REFUSED:
                outcome = "risk_refused"
                raise ForbiddenError(
                    ApplicationErrorCode.MOVEMENT_REFUSED,
                    details=self._refusal_details(risk_decision),
                )
            if not risk_decision.available and not self._settings.risk_fail_open:
                outcome = "risk_unavailable"
                raise DependencyUnavailableError(
                    ApplicationErrorCode.DEPENDENCY_UNAVAILABLE,
                    details={"dependency": "risk"},
                )

            result = await self._movement.apply(
                MovementRequest(
                    wallet_id=wallet_id,
                    owner_id=owner_id,
                    amount=amount,
                    direction=self._direction,
                    description=description,
                    idempotency_key=idempotency_key,
                )
            )
            outcome = "replayed" if result.replayed else "success"
            if not risk_decision.available:
                # Money moved without being assessed. Distinct from `success`
                # in the metric, because "we allowed it" and "we could not
                # check and allowed it anyway" must be countable separately —
                # the second one is what an incident review asks for.
                outcome = "success_unassessed"
            return MovementResult(
                entry_id=result.entry.id_,
                wallet_id=result.wallet.id_,
                balance=result.wallet.balance,
                replayed=result.replayed,
            )
        except ApplicationError as failure:
            # Labelled by its code rather than left at "error". A wallet that
            # was busy and a lost optimistic-version race are ordinary, expected
            # outcomes under contention; filed under "error" they are
            # indistinguishable from a bug, and a load run reports a healthy
            # system as a broken one. Found exactly that way — see the numbers
            # in tools/load/README.md.
            if outcome == "error":
                outcome = failure.code.value.lower()
            raise
        finally:
            self._metrics.observe_interactor(
                name=self._metric_name,
                outcome=outcome,
                duration_seconds=time.perf_counter() - started_at,
            )

    @staticmethod
    def _refusal_details(decision: RiskDecision) -> dict[str, str] | None:
        """The provider's reference, and nothing else.

        `reason` stays out of the response on purpose. It is prose written by a
        third party, it may describe the rule that fired, and telling a caller
        which rule refused them is how a refusal becomes a tuning signal for
        whoever is probing.
        """
        return {"reference": decision.reference} if decision.reference else None


class DepositToWalletInteractor(_MovementInteractor):
    _metric_name = "deposit_to_wallet"
    _direction = LedgerDirection.CREDIT

    async def __call__(self, command: DepositCommand) -> MovementResult:
        return await self._run(
            wallet_id=command.wallet_id,
            owner_id=command.owner_id,
            amount=command.amount,
            description=command.description or "deposit",
            idempotency_key=command.idempotency_key,
        )


class WithdrawFromWalletInteractor(_MovementInteractor):
    _metric_name = "withdraw_from_wallet"
    _direction = LedgerDirection.DEBIT

    async def __call__(self, command: WithdrawCommand) -> MovementResult:
        return await self._run(
            wallet_id=command.wallet_id,
            owner_id=command.owner_id,
            amount=command.amount,
            description=command.description or "withdrawal",
            idempotency_key=command.idempotency_key,
        )
