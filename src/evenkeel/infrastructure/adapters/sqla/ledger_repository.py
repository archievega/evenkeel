from typing import Any
from uuid import UUID

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from evenkeel.application.ports import Page
from evenkeel.domain.entities.ledger_entry import LedgerDirection, LedgerEntry
from evenkeel.domain.value_objects.ids import LedgerEntryId, OwnerId, WalletId
from evenkeel.domain.value_objects.money import CurrencyCode, Money
from evenkeel.infrastructure.adapters.sqla.cursor import decode_cursor, encode_cursor
from evenkeel.infrastructure.sqla.tables import ledger_entries_table, wallets_table


def _to_entity(row: Row[Any]) -> LedgerEntry:
    data = row._mapping
    currency = CurrencyCode(data["currency"])
    return LedgerEntry(
        id_=LedgerEntryId(data["id"]),
        wallet_id=WalletId(data["wallet_id"]),
        direction=LedgerDirection(data["direction"]),
        amount=Money(amount=data["amount"], currency=currency),
        balance_after=Money(amount=data["balance_after"], currency=currency),
        description=data["description"],
        occurred_at=data["occurred_at"],
    )


class SqlaLedgerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, entry: LedgerEntry) -> None:
        await self._session.execute(
            ledger_entries_table.insert().values(
                id=entry.id_.value,
                wallet_id=entry.wallet_id.value,
                direction=entry.direction.value,
                amount=entry.amount.amount,
                currency=entry.amount.currency.value,
                balance_after=entry.balance_after.amount,
                description=entry.description,
                occurred_at=entry.occurred_at,
            )
        )

    async def read(
        self, entry_id: LedgerEntryId, owner_id: OwnerId
    ) -> LedgerEntry | None:
        row = (
            await self._session.execute(
                select(ledger_entries_table)
                .join(
                    wallets_table, wallets_table.c.id == ledger_entries_table.c.wallet_id
                )
                .where(ledger_entries_table.c.id == entry_id.value)
                .where(wallets_table.c.owner_id == owner_id.value)
            )
        ).one_or_none()
        return None if row is None else _to_entity(row)

    async def list_by_wallet(
        self,
        wallet_id: WalletId,
        owner_id: OwnerId,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> Page[LedgerEntry]:
        statement = (
            select(ledger_entries_table)
            .join(wallets_table, wallets_table.c.id == ledger_entries_table.c.wallet_id)
            .where(ledger_entries_table.c.wallet_id == wallet_id.value)
            .where(wallets_table.c.owner_id == owner_id.value)
            .order_by(ledger_entries_table.c.id.desc())
            .limit(limit + 1)
        )
        after = decode_cursor(cursor)
        if after is not None:
            statement = statement.where(ledger_entries_table.c.id < after)

        rows = (await self._session.execute(statement)).all()
        has_more = len(rows) > limit
        visible = rows[:limit]
        next_cursor = (
            encode_cursor(UUID(str(visible[-1]._mapping["id"])))
            if has_more and visible
            else None
        )
        return Page(items=[_to_entity(row) for row in visible], next_cursor=next_cursor)
