from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from evenkeel.domain.entities.wallet import Wallet, WalletStatus
from evenkeel.domain.errors import DomainError, DomainErrorCode
from evenkeel.domain.value_objects.ids import OwnerId, WalletId
from evenkeel.domain.value_objects.money import CurrencyCode, Money

NOW = datetime(2026, 1, 1, tzinfo=UTC)
EUR = CurrencyCode("EUR")
USD = CurrencyCode("USD")


def make_wallet(balance: str = "0.00") -> Wallet:
    wallet = Wallet.open(
        id_=WalletId(uuid4()), owner_id=OwnerId(uuid4()), currency=EUR, now=NOW
    )
    if balance != "0.00":
        wallet.deposit(Money(amount=Decimal(balance), currency=EUR), now=NOW)
    return wallet


def test_withdrawal_beyond_balance_is_refused() -> None:
    wallet = make_wallet("10.00")

    with pytest.raises(DomainError) as error:
        wallet.withdraw(Money(amount=Decimal("10.01"), currency=EUR), now=NOW)

    assert error.value.code is DomainErrorCode.WALLET_INSUFFICIENT_FUNDS
    assert wallet.balance.amount == Decimal("10.00")


def test_balance_survives_a_repeated_fractional_amount() -> None:
    """The float-drift regression, stated as a test.

    0.1 has no exact binary representation, so a float balance ends up at
    9.999999999999998 and the wallet can never be emptied.
    """
    wallet = make_wallet("1.00")
    for _ in range(10):
        wallet.withdraw(Money(amount=Decimal("0.10"), currency=EUR), now=NOW)

    assert wallet.balance.amount == Decimal("0.00")
    assert wallet.balance.is_zero()


def test_mixing_currencies_raises_instead_of_adding() -> None:
    wallet = make_wallet("10.00")

    with pytest.raises(DomainError) as error:
        wallet.deposit(Money(amount=Decimal("5.00"), currency=USD), now=NOW)

    assert error.value.code is DomainErrorCode.MONEY_CURRENCY_MISMATCH


def test_every_movement_advances_the_version() -> None:
    wallet = make_wallet()
    assert wallet.version == 0

    wallet.deposit(Money(amount=Decimal("1.00"), currency=EUR), now=NOW)

    assert wallet.version == 1


@pytest.mark.parametrize("amount", ["0.00", "-1.00"])
def test_non_positive_movements_are_refused(amount: str) -> None:
    wallet = make_wallet("10.00")

    with pytest.raises(DomainError) as error:
        wallet.deposit(Money(amount=Decimal(amount), currency=EUR), now=NOW)

    assert error.value.code is DomainErrorCode.MONEY_AMOUNT_NOT_POSITIVE


def test_sub_cent_amounts_are_refused() -> None:
    with pytest.raises(DomainError) as error:
        Money(amount=Decimal("1.005"), currency=EUR)

    assert error.value.code is DomainErrorCode.MONEY_SCALE_EXCEEDED


def test_closed_wallet_accepts_nothing() -> None:
    wallet = make_wallet()
    wallet.close(now=NOW)
    assert wallet.status is WalletStatus.CLOSED

    with pytest.raises(DomainError) as error:
        wallet.deposit(Money(amount=Decimal("1.00"), currency=EUR), now=NOW)

    assert error.value.code is DomainErrorCode.WALLET_CLOSED


def test_wallet_with_a_balance_cannot_be_closed() -> None:
    wallet = make_wallet("5.00")

    with pytest.raises(DomainError) as error:
        wallet.close(now=NOW)

    assert error.value.code is DomainErrorCode.WALLET_NOT_EMPTY


def test_identity_cannot_be_reassigned() -> None:
    wallet = make_wallet()

    with pytest.raises(DomainError) as error:
        wallet.id_ = WalletId(uuid4())

    assert error.value.code is DomainErrorCode.IDENTITY_IS_IMMUTABLE


def test_currency_code_rejects_malformed_input() -> None:
    for bad in ["eur", "EURO", "E1R", ""]:
        with pytest.raises(DomainError):
            CurrencyCode(bad)


def test_a_negative_balance_cannot_be_constructed_at_all() -> None:
    """The read path runs this constructor, which is the whole of ADR 2.

    An ORM that rebuilds rows through `__new__` skips it, and one of the
    audited codebases loaded negative balances out of its database for years
    without complaint. The column carries a CHECK too; this is the half that
    survives a restore from backup or a hand-written UPDATE.
    """
    with pytest.raises(DomainError) as failure:
        Wallet(
            id_=WalletId(uuid4()),
            owner_id=OwnerId(uuid4()),
            balance=Money(amount=Decimal("-0.01"), currency=EUR),
            status=WalletStatus.OPEN,
            version=3,
            created_at=NOW,
            updated_at=NOW,
        )

    assert failure.value.code is DomainErrorCode.WALLET_BALANCE_NEGATIVE
