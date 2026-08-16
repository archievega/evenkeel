from decimal import Decimal
from uuid import uuid4

import pytest

from evenkeel.application.errors import (
    ApplicationErrorCode,
    ConflictError,
    NotFoundError,
)
from evenkeel.application.services.wallet_movement import (
    MovementRequest,
    WalletMovementService,
    WalletMovementSettings,
)
from evenkeel.domain.entities.ledger_entry import LedgerDirection
from evenkeel.domain.entities.wallet import Wallet
from evenkeel.domain.errors import DomainError, DomainErrorCode
from evenkeel.domain.value_objects.ids import OwnerId, WalletId
from evenkeel.domain.value_objects.money import CurrencyCode, Money
from evenkeel.infrastructure.adapters.memory.idempotency import InMemoryIdempotencyStore
from evenkeel.infrastructure.adapters.memory.locking import InMemoryDistributedLock
from evenkeel.infrastructure.adapters.noop.metrics import NoopMetrics
from tests.fakes.repositories import (
    FakeLedgerRepository,
    FakeTransactionManager,
    FakeWalletRepository,
)
from tests.fakes.system import BusyLock, FixedClock, SequentialIdGenerator

EUR = CurrencyCode("EUR")


class Harness:
    def __init__(self, *, balance: str = "100.00", lock: object | None = None) -> None:
        self.clock = FixedClock()
        self.owner_id = OwnerId(uuid4())
        self.wallet = Wallet.open(
            id_=WalletId(uuid4()),
            owner_id=self.owner_id,
            currency=EUR,
            now=self.clock.now(),
        )
        if balance != "0.00":
            self.wallet.deposit(
                Money(amount=Decimal(balance), currency=EUR), now=self.clock.now()
            )
        self.wallets = FakeWalletRepository([self.wallet])
        self.ledger = FakeLedgerRepository()
        self.transaction_manager = FakeTransactionManager()
        self.service = WalletMovementService(
            wallets=self.wallets,
            ledger=self.ledger,
            transaction_manager=self.transaction_manager,
            distributed_lock=lock or InMemoryDistributedLock(),
            idempotency=InMemoryIdempotencyStore(),
            clock=self.clock,
            id_generator=SequentialIdGenerator(),
            metrics=NoopMetrics(),
            settings=WalletMovementSettings(),
        )

    def request(self, amount: str, **overrides: object) -> MovementRequest:
        defaults: dict[str, object] = {
            "wallet_id": self.wallet.id_,
            "owner_id": self.owner_id,
            "amount": Money(amount=Decimal(amount), currency=EUR),
            "direction": LedgerDirection.DEBIT,
            "description": "test",
            "idempotency_key": None,
        }
        defaults.update(overrides)
        return MovementRequest(**defaults)  # type: ignore[arg-type]


async def test_successful_withdrawal_commits_once_and_writes_one_entry() -> None:
    harness = Harness()

    outcome = await harness.service.apply(harness.request("30.00"))

    assert outcome.wallet.balance.amount == Decimal("70.00")
    assert harness.transaction_manager.commits == 1
    assert len(harness.ledger.entries) == 1


async def test_the_write_path_locks_the_row() -> None:
    harness = Harness()

    await harness.service.apply(harness.request("1.00"))

    assert harness.wallets.for_update_calls == 1


async def test_retrying_with_the_same_key_does_not_move_money_twice() -> None:
    harness = Harness()
    request = harness.request("40.00", idempotency_key="abc-123")

    first = await harness.service.apply(request)
    second = await harness.service.apply(request)

    assert first.replayed is False
    assert second.replayed is True
    assert second.entry.id_ == first.entry.id_
    assert harness.wallet.balance.amount == Decimal("60.00")
    assert len(harness.ledger.entries) == 1


async def test_reusing_a_key_for_a_different_amount_is_rejected() -> None:
    harness = Harness()
    await harness.service.apply(harness.request("10.00", idempotency_key="dup"))

    with pytest.raises(ConflictError) as error:
        await harness.service.apply(harness.request("99.00", idempotency_key="dup"))

    assert error.value.code is ApplicationErrorCode.IDEMPOTENCY_KEY_REUSED


async def test_a_concurrent_writer_loses_instead_of_overwriting() -> None:
    """The lost-update scenario, made deterministic.

    Another process committed a change after this one loaded the wallet, so the
    row version no longer matches. Without this guard both writers would commit
    and one balance change would silently vanish.
    """
    harness = Harness()
    harness.wallets.force_version(harness.wallet.id_, 99)

    with pytest.raises(ConflictError) as error:
        await harness.service.apply(harness.request("10.00"))

    assert error.value.code is ApplicationErrorCode.WALLET_VERSION_CONFLICT
    assert harness.transaction_manager.rollbacks == 1
    assert harness.transaction_manager.commits == 0


async def test_a_contended_wallet_reports_busy_rather_than_waiting_forever() -> None:
    harness = Harness(lock=BusyLock())

    with pytest.raises(ConflictError) as error:
        await harness.service.apply(harness.request("10.00"))

    assert error.value.code is ApplicationErrorCode.WALLET_BUSY
    assert harness.transaction_manager.commits == 0


async def test_another_owner_cannot_touch_the_wallet() -> None:
    """Scoping happens in the query, so a foreign wallet simply does not exist."""
    harness = Harness()

    with pytest.raises(NotFoundError) as error:
        await harness.service.apply(harness.request("10.00", owner_id=OwnerId(uuid4())))

    assert error.value.code is ApplicationErrorCode.WALLET_NOT_FOUND
    assert harness.transaction_manager.commits == 0


async def test_a_failed_movement_leaves_the_balance_untouched() -> None:
    harness = Harness(balance="5.00")

    with pytest.raises(DomainError) as error:
        await harness.service.apply(harness.request("50.00"))

    assert error.value.code is DomainErrorCode.WALLET_INSUFFICIENT_FUNDS

    assert harness.wallet.balance.amount == Decimal("5.00")
    assert len(harness.ledger.entries) == 0
