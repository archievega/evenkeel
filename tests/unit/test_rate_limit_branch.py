"""The rate-limit branch of the movement interactor, gap 3 in docs/SECURITY_CONTROLS.md.

The limiter itself is covered by the port contract suite. What was unproven is
what the use case does when it says no — and that is the part with consequences:
a refusal that still moved money, or one that reported success, would both pass
every test that existed before this file.

`DenyingRateLimiter` had been sitting in `tests/fakes/system.py` used by nothing.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from evenkeel.application.errors import ApplicationErrorCode, RateLimitedError
from evenkeel.application.interactors.wallets import (
    DepositCommand,
    DepositToWalletInteractor,
    MoveMoneySettings,
    WithdrawCommand,
    WithdrawFromWalletInteractor,
)
from evenkeel.domain.value_objects.ids import WalletId
from evenkeel.domain.value_objects.money import Money
from tests.fakes.system import DenyingRateLimiter, ScriptedRiskAssessment
from tests.unit.test_wallet_movement import EUR, Harness


def deposit_interactor(harness: Harness) -> DepositToWalletInteractor:
    return DepositToWalletInteractor(
        movement=harness.service,
        rate_limiter=DenyingRateLimiter(),
        risk=ScriptedRiskAssessment(),
        metrics=harness.service._metrics,
        settings=MoveMoneySettings(),
    )


def withdraw_interactor(harness: Harness) -> WithdrawFromWalletInteractor:
    return WithdrawFromWalletInteractor(
        movement=harness.service,
        rate_limiter=DenyingRateLimiter(),
        risk=ScriptedRiskAssessment(),
        metrics=harness.service._metrics,
        settings=MoveMoneySettings(),
    )


def amount(value: str) -> Money:
    return Money(amount=Decimal(value), currency=EUR)


async def test_a_denied_deposit_raises_rate_limited() -> None:
    harness = Harness()

    with pytest.raises(RateLimitedError) as error:
        await deposit_interactor(harness)(
            DepositCommand(
                wallet_id=harness.wallet.id_,
                owner_id=harness.owner_id,
                amount=amount("10.00"),
            )
        )

    assert error.value.code is ApplicationErrorCode.RATE_LIMITED
    assert error.value.status_code == 429


async def test_a_denied_request_moves_no_money() -> None:
    """The assertion that matters.

    A limiter that refuses after the write has already happened is worse than
    no limiter: the client is told to back off from an operation that took
    effect.
    """
    harness = Harness()
    before = harness.wallet.balance.amount

    with pytest.raises(RateLimitedError):
        await withdraw_interactor(harness)(
            WithdrawCommand(
                wallet_id=harness.wallet.id_,
                owner_id=harness.owner_id,
                amount=amount("10.00"),
            )
        )

    assert harness.wallet.balance.amount == before
    assert harness.ledger.entries == {}
    assert harness.transaction_manager.commits == 0


async def test_the_refusal_tells_the_client_when_to_retry() -> None:
    harness = Harness()

    with pytest.raises(RateLimitedError) as error:
        await deposit_interactor(harness)(
            DepositCommand(
                wallet_id=harness.wallet.id_,
                owner_id=harness.owner_id,
                amount=amount("1.00"),
            )
        )

    assert error.value.details["retry_after_seconds"] == 42.0


async def test_the_limiter_runs_before_the_wallet_is_even_read() -> None:
    """Shedding load must not cost a database round trip.

    If the limit were checked after loading the wallet, a flood would still
    reach the database — which is the load the limiter exists to prevent.
    """
    harness = Harness()

    with pytest.raises(RateLimitedError):
        await deposit_interactor(harness)(
            DepositCommand(
                wallet_id=WalletId(uuid4()),
                owner_id=harness.owner_id,
                amount=amount("1.00"),
            )
        )

    assert harness.wallets.for_update_calls == 0
