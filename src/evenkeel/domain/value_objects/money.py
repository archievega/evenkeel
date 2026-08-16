from dataclasses import dataclass
from decimal import Decimal

from evenkeel.domain.base.value_object import ValueObject
from evenkeel.domain.errors import DomainError, DomainErrorCode

MONEY_SCALE = 2


@dataclass(frozen=True, slots=True, repr=False)
class CurrencyCode(ValueObject):
    value: str

    def _validate(self) -> None:
        if len(self.value) != 3 or not self.value.isalpha() or not self.value.isupper():
            raise DomainError(
                DomainErrorCode.CURRENCY_CODE_INVALID,
                {"value": self.value},
            )


@dataclass(frozen=True, slots=True, repr=False)
class Money(ValueObject):
    """An amount in a single currency.

    Money is ``Decimal`` and never ``float``: binary floats cannot represent
    0.01 exactly, so float balances drift by fractions of a cent per operation
    and eventually stop reconciling. Arithmetic across currencies raises rather
    than silently adding unrelated units.
    """

    amount: Decimal
    currency: CurrencyCode

    def _validate(self) -> None:
        if -self.amount.as_tuple().exponent > MONEY_SCALE:  # type: ignore[operator]
            raise DomainError(
                DomainErrorCode.MONEY_SCALE_EXCEEDED,
                {"amount": str(self.amount), "max_scale": MONEY_SCALE},
            )

    @classmethod
    def zero(cls, currency: CurrencyCode) -> "Money":
        return cls(amount=Decimal("0.00"), currency=currency)

    def _assert_same_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise DomainError(
                DomainErrorCode.MONEY_CURRENCY_MISMATCH,
                {"left": self.currency.value, "right": other.currency.value},
            )

    def require_positive(self) -> "Money":
        if self.amount <= 0:
            raise DomainError(
                DomainErrorCode.MONEY_AMOUNT_NOT_POSITIVE,
                {"amount": str(self.amount)},
            )
        return self

    def __add__(self, other: "Money") -> "Money":
        self._assert_same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._assert_same_currency(other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._assert_same_currency(other)
        return self.amount < other.amount

    def is_zero(self) -> bool:
        return self.amount == 0
