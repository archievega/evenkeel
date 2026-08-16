from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from appcore.domain.entities.ledger_entry import LedgerEntry
from appcore.domain.entities.wallet import Wallet


class OpenWalletRequest(BaseModel):
    currency: str = Field(min_length=3, max_length=3, examples=["EUR"])


class MoneyRequest(BaseModel):
    # gt=0 duplicates a domain rule on purpose: the schema rejects nonsense at
    # the edge with a helpful 422, while the domain keeps the invariant true for
    # every caller, including background jobs that never touch HTTP.
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    description: str = Field(default="", max_length=200)


class WalletResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    balance: Decimal
    currency: str
    status: str
    version: int

    @classmethod
    def of(cls, wallet: Wallet) -> "WalletResponse":
        return cls(
            id=wallet.id_.value,
            owner_id=wallet.owner_id.value,
            balance=wallet.balance.amount,
            currency=wallet.balance.currency.value,
            status=wallet.status.value,
            version=wallet.version,
        )


class LedgerEntryResponse(BaseModel):
    id: UUID
    direction: str
    amount: Decimal
    currency: str
    balance_after: Decimal
    description: str
    occurred_at: str

    @classmethod
    def of(cls, entry: LedgerEntry) -> "LedgerEntryResponse":
        return cls(
            id=entry.id_.value,
            direction=entry.direction.value,
            amount=entry.amount.amount,
            currency=entry.amount.currency.value,
            balance_after=entry.balance_after.amount,
            description=entry.description,
            occurred_at=entry.occurred_at.isoformat(),
        )


class MovementResponse(BaseModel):
    entry_id: UUID
    wallet_id: UUID
    balance: Decimal
    currency: str
    replayed: bool


class WalletPageResponse(BaseModel):
    items: list[WalletResponse]
    next_cursor: str | None


class LedgerPageResponse(BaseModel):
    items: list[LedgerEntryResponse]
    next_cursor: str | None
