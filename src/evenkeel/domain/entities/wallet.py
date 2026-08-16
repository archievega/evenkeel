from datetime import datetime
from enum import StrEnum

from evenkeel.domain.base.entity import Entity
from evenkeel.domain.errors import DomainError, DomainErrorCode
from evenkeel.domain.value_objects.ids import OwnerId, WalletId
from evenkeel.domain.value_objects.money import CurrencyCode, Money


class WalletStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class Wallet(Entity[WalletId]):
    """Aggregate root guarding the balance invariants.

    ``version`` backs optimistic concurrency: the repository writes with a
    ``WHERE version = :loaded_version`` predicate, so two concurrent withdrawals
    that both read the same balance cannot both commit. That is the cheap half
    of the defence; the distributed lock in the interactor is the other half.
    """

    def __init__(
        self,
        *,
        id_: WalletId,
        owner_id: OwnerId,
        balance: Money,
        status: WalletStatus,
        version: int,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        super().__init__(id_=id_)
        self.owner_id = owner_id
        self.balance = balance
        self.status = status
        self.version = version
        self.created_at = created_at
        self.updated_at = updated_at

    @classmethod
    def open(
        cls,
        *,
        id_: WalletId,
        owner_id: OwnerId,
        currency: CurrencyCode,
        now: datetime,
    ) -> "Wallet":
        return cls(
            id_=id_,
            owner_id=owner_id,
            balance=Money.zero(currency),
            status=WalletStatus.OPEN,
            version=0,
            created_at=now,
            updated_at=now,
        )

    @property
    def currency(self) -> CurrencyCode:
        return self.balance.currency

    def is_owned_by(self, owner_id: OwnerId) -> bool:
        return self.owner_id == owner_id

    def _require_open(self) -> None:
        if self.status is not WalletStatus.OPEN:
            raise DomainError(
                DomainErrorCode.WALLET_CLOSED,
                {"wallet_id": str(self.id_.value)},
            )

    def _touch(self, now: datetime) -> None:
        self.version += 1
        self.updated_at = now

    def deposit(self, amount: Money, *, now: datetime) -> Money:
        self._require_open()
        amount.require_positive()
        self.balance = self.balance + amount
        self._touch(now)
        return self.balance

    def withdraw(self, amount: Money, *, now: datetime) -> Money:
        self._require_open()
        amount.require_positive()
        if self.balance < amount:
            raise DomainError(
                DomainErrorCode.WALLET_INSUFFICIENT_FUNDS,
                {
                    "wallet_id": str(self.id_.value),
                    "balance": str(self.balance.amount),
                    "requested": str(amount.amount),
                },
            )
        self.balance = self.balance - amount
        self._touch(now)
        return self.balance

    def close(self, *, now: datetime) -> None:
        self._require_open()
        if not self.balance.is_zero():
            raise DomainError(
                DomainErrorCode.WALLET_NOT_EMPTY,
                {"wallet_id": str(self.id_.value), "balance": str(self.balance.amount)},
            )
        self.status = WalletStatus.CLOSED
        self._touch(now)
