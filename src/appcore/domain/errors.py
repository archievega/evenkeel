from enum import StrEnum
from typing import Any


class DomainErrorCode(StrEnum):
    BASE_CLASS_INSTANTIATION = "BASE_CLASS_INSTANTIATION"
    IDENTITY_IS_IMMUTABLE = "IDENTITY_IS_IMMUTABLE"
    EMPTY_VALUE_OBJECT = "EMPTY_VALUE_OBJECT"

    CURRENCY_CODE_INVALID = "CURRENCY_CODE_INVALID"
    MONEY_CURRENCY_MISMATCH = "MONEY_CURRENCY_MISMATCH"
    MONEY_AMOUNT_NOT_POSITIVE = "MONEY_AMOUNT_NOT_POSITIVE"
    MONEY_SCALE_EXCEEDED = "MONEY_SCALE_EXCEEDED"

    WALLET_INSUFFICIENT_FUNDS = "WALLET_INSUFFICIENT_FUNDS"
    WALLET_CLOSED = "WALLET_CLOSED"
    WALLET_NOT_EMPTY = "WALLET_NOT_EMPTY"


class DomainError(Exception):
    """Raised when a domain invariant is violated.

    Carries a stable machine-readable code plus optional structured context.
    It deliberately knows nothing about HTTP: mapping a code to a transport
    status is the presentation layer's job.
    """

    def __init__(
        self,
        code: DomainErrorCode,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.context = context or {}
        super().__init__(code.value)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code.value!r}, context={self.context!r})"
        )
