from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from evenkeel.domain.entities.ledger_entry import LedgerEntry
from evenkeel.domain.entities.wallet import Wallet


class OpenWalletRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"currency": "EUR"}]})

    currency: str = Field(
        min_length=3,
        max_length=3,
        description="ISO 4217 alphabetic code, uppercase. Fixed for the wallet's life.",
        examples=["EUR"],
    )


class MoneyRequest(BaseModel):
    # gt=0 duplicates a domain rule on purpose: the schema rejects nonsense at
    # the edge with a helpful 422, while the domain keeps the invariant true for
    # every caller, including background jobs that never touch HTTP.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"amount": "100.00", "currency": "EUR", "description": "salary"}]
        }
    )

    amount: Decimal = Field(
        gt=0,
        max_digits=20,
        decimal_places=2,
        description=(
            "Positive, at most two decimal places. Sent as a JSON string so no "
            "float rounding happens in transit."
        ),
        examples=["100.00"],
    )
    currency: str = Field(
        min_length=3,
        max_length=3,
        description="Must match the wallet's currency; a mismatch is refused, not converted.",
        examples=["EUR"],
    )
    description: str = Field(
        default="",
        max_length=200,
        description=(
            "Free text recorded on the ledger entry. Part of the idempotency "
            "fingerprint: the same key with a different description is a "
            "different request."
        ),
        examples=["salary"],
    )


class WalletResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="UUIDv7, so id order is creation order.")
    owner_id: UUID = Field(description="The authenticated caller. Always yours.")
    balance: Decimal = Field(
        description="Decimal string, never a JSON number.", examples=["100.00"]
    )
    currency: str = Field(description="ISO 4217.", examples=["EUR"])
    status: str = Field(
        description="`open` or `closed`. A closed wallet refuses movements.",
        examples=["open"],
    )
    version: int = Field(
        description=(
            "Optimistic concurrency counter, incremented by every movement. "
            "Exposed because it is the number that explains a 409."
        )
    )

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
    id: UUID = Field(description="UUIDv7. Also the pagination sort key.")
    direction: str = Field(
        description="`credit` for a deposit, `debit` for a withdrawal.",
        examples=["credit"],
    )
    amount: Decimal = Field(description="Always positive; the sign is the direction.")
    currency: str = Field(description="ISO 4217.", examples=["EUR"])
    balance_after: Decimal = Field(
        description=(
            "The balance this entry produced. A ledger states what the balance "
            "*was*, which is what makes it auditable — reconstructing it from "
            "the current balance and a sum is how discrepancies hide."
        )
    )
    description: str = Field(description="Whatever the caller sent, or a default.")
    occurred_at: str = Field(
        description="RFC 3339, always UTC and always offset-aware.",
        examples=["2026-08-16T12:34:56.789012+00:00"],
    )

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
    entry_id: UUID = Field(description="The ledger entry this movement wrote.")
    wallet_id: UUID = Field(description="The wallet this movement was applied to.")
    balance: Decimal = Field(
        description=(
            "Balance after this movement. On a replay it is the balance the "
            "original was written with, not the wallet's balance now."
        ),
        examples=["100.00"],
    )
    currency: str = Field(description="ISO 4217.", examples=["EUR"])
    replayed: bool = Field(
        description=(
            "True when an idempotency key matched an earlier request. Nothing "
            "was applied a second time; the entry is the original one."
        )
    )


class WalletPageResponse(BaseModel):
    items: list[WalletResponse] = Field(description="Newest first.")
    next_cursor: str | None = Field(
        description=(
            "Pass as `cursor` for the next page. `null` means this was the "
            "last one. Opaque: it encodes a position, not an offset, so rows "
            "inserted while you page are neither skipped nor repeated."
        )
    )


class LedgerPageResponse(BaseModel):
    items: list[LedgerEntryResponse] = Field(description="Newest first.")
    next_cursor: str | None = Field(
        description=(
            "Pass as `cursor` for the next page. `null` means this was the "
            "last one. Opaque: it encodes a position, not an offset, so rows "
            "inserted while you page are neither skipped nor repeated."
        )
    )
