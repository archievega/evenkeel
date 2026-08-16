from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from evenkeel.application.ports import Page
from evenkeel.domain.entities.wallet import Wallet, WalletStatus
from evenkeel.domain.value_objects.ids import OwnerId, WalletId
from evenkeel.domain.value_objects.money import CurrencyCode, Money
from evenkeel.infrastructure.adapters.sqla.cursor import decode_cursor, encode_cursor
from evenkeel.infrastructure.sqla.tables import wallets_table


def _to_entity(row: Row[Any] | Mapping[str, Any]) -> Wallet:
    data = row._mapping if isinstance(row, Row) else row
    currency = CurrencyCode(data["currency"])
    return Wallet(
        id_=WalletId(data["id"]),
        owner_id=OwnerId(data["owner_id"]),
        balance=Money(amount=data["balance_amount"], currency=currency),
        status=WalletStatus(data["status"]),
        version=data["version"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


class SqlaWalletRepository:
    """Explicit row-to-entity translation over SQLAlchemy Core.

    The domain entity is not ORM-mapped, so persistence concerns never leak
    into it and a write only happens where a repository method says it does --
    no implicit flush of a mutated object at an unrelated await point.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def read(
        self,
        wallet_id: WalletId,
        owner_id: OwnerId,
        *,
        for_update: bool = False,
    ) -> Wallet | None:
        statement = (
            select(wallets_table)
            .where(wallets_table.c.id == wallet_id.value)
            .where(wallets_table.c.owner_id == owner_id.value)
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).one_or_none()
        return None if row is None else _to_entity(row)

    async def list_by_owner(
        self,
        owner_id: OwnerId,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> Page[Wallet]:
        statement = (
            select(wallets_table)
            .where(wallets_table.c.owner_id == owner_id.value)
            .order_by(wallets_table.c.id.desc())
            .limit(limit + 1)
        )
        after = decode_cursor(cursor)
        if after is not None:
            statement = statement.where(wallets_table.c.id < after)

        rows = (await self._session.execute(statement)).all()
        return _paginate(rows, limit)

    async def add(self, wallet: Wallet) -> None:
        await self._session.execute(wallets_table.insert().values(_to_row(wallet)))

    async def update(self, wallet: Wallet, *, expected_version: int) -> bool:
        values = _to_row(wallet)
        del values["id"]
        del values["created_at"]
        result = await self._session.execute(
            wallets_table.update()
            .where(wallets_table.c.id == wallet.id_.value)
            .where(wallets_table.c.version == expected_version)
            .values(values)
        )
        # An UPDATE always yields a CursorResult; the executor's return type is
        # the broader Result, so the row count is not visible statically.
        return cast(CursorResult[Any], result).rowcount == 1


def _to_row(wallet: Wallet) -> dict[str, Any]:
    return {
        "id": wallet.id_.value,
        "owner_id": wallet.owner_id.value,
        "balance_amount": wallet.balance.amount,
        "currency": wallet.balance.currency.value,
        "status": wallet.status.value,
        "version": wallet.version,
        "created_at": wallet.created_at,
        "updated_at": wallet.updated_at,
    }


def _paginate(rows: Sequence[Row[Any]], limit: int) -> Page[Wallet]:
    has_more = len(rows) > limit
    visible = rows[:limit]
    items = [_to_entity(row) for row in visible]
    next_cursor = (
        encode_cursor(UUID(str(visible[-1]._mapping["id"])))
        if has_more and visible
        else None
    )
    return Page(items=items, next_cursor=next_cursor)
