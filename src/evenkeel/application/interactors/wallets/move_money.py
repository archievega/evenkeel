import time
from dataclasses import dataclass

from evenkeel.application.errors import ApplicationErrorCode, RateLimitedError
from evenkeel.application.ports import (
    MetricsPort,
    RateLimiterPort,
    RateLimitPolicy,
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
        metrics: MetricsPort,
        settings: MoveMoneySettings,
    ) -> None:
        self._movement = movement
        self._rate_limiter = rate_limiter
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
            return MovementResult(
                entry_id=result.entry.id_,
                wallet_id=result.wallet.id_,
                balance=result.wallet.balance,
                replayed=result.replayed,
            )
        finally:
            self._metrics.observe_interactor(
                name=self._metric_name,
                outcome=outcome,
                duration_seconds=time.perf_counter() - started_at,
            )


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
