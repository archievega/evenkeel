from datetime import datetime
from enum import StrEnum

from evenkeel.domain.base.entity import Entity
from evenkeel.domain.value_objects.ids import LedgerEntryId, WalletId
from evenkeel.domain.value_objects.money import Money


class LedgerDirection(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"


class LedgerEntry(Entity[LedgerEntryId]):
    """Append-only record of a single balance movement.

    Entries are never mutated or deleted: the wallet balance is a cache of the
    entry history, which is what makes the ledger auditable and lets a
    reconciliation job detect drift by replaying entries.
    """

    def __init__(
        self,
        *,
        id_: LedgerEntryId,
        wallet_id: WalletId,
        direction: LedgerDirection,
        amount: Money,
        balance_after: Money,
        description: str,
        occurred_at: datetime,
    ) -> None:
        super().__init__(id_=id_)
        self.wallet_id = wallet_id
        self.direction = direction
        self.amount = amount
        self.balance_after = balance_after
        self.description = description
        self.occurred_at = occurred_at

    @classmethod
    def record(
        cls,
        *,
        id_: LedgerEntryId,
        wallet_id: WalletId,
        direction: LedgerDirection,
        amount: Money,
        balance_after: Money,
        description: str,
        now: datetime,
    ) -> "LedgerEntry":
        return cls(
            id_=id_,
            wallet_id=wallet_id,
            direction=direction,
            amount=amount,
            balance_after=balance_after,
            description=description.strip(),
            occurred_at=now,
        )
