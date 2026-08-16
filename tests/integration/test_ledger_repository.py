"""Ledger reads are scoped through a join, so they need a real database to prove."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from evenkeel.domain.entities.ledger_entry import LedgerDirection, LedgerEntry
from evenkeel.domain.entities.wallet import Wallet
from evenkeel.domain.value_objects.ids import LedgerEntryId, OwnerId, WalletId
from evenkeel.domain.value_objects.money import CurrencyCode, Money
from evenkeel.infrastructure.adapters.sqla.ledger_repository import SqlaLedgerRepository
from evenkeel.infrastructure.adapters.sqla.wallet_repository import SqlaWalletRepository

pytestmark = pytest.mark.integration

EUR = CurrencyCode("EUR")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def seed_wallet(
    wallets: SqlaWalletRepository, owner: OwnerId, balance: str = "100.00"
) -> Wallet:
    wallet = Wallet.open(id_=WalletId(uuid4()), owner_id=owner, currency=EUR, now=NOW)
    wallet.deposit(Money(amount=Decimal(balance), currency=EUR), now=NOW)
    await wallets.add(wallet)
    return wallet


def entry(wallet: Wallet, amount: str) -> LedgerEntry:
    money = Money(amount=Decimal(amount), currency=EUR)
    return LedgerEntry.record(
        id_=LedgerEntryId(uuid4()),
        wallet_id=wallet.id_,
        direction=LedgerDirection.CREDIT,
        amount=money,
        balance_after=money,
        description="seed",
        now=NOW,
    )


class TestLedgerOwnerScoping:
    async def test_the_owner_reads_their_entries(
        self,
        wallets: SqlaWalletRepository,
        ledger: SqlaLedgerRepository,
        session: AsyncSession,
    ) -> None:
        owner = OwnerId(uuid4())
        wallet = await seed_wallet(wallets, owner)
        await ledger.add(entry(wallet, "10.00"))
        await session.commit()

        page = await ledger.list_by_wallet(wallet.id_, owner, limit=10)

        assert len(page.items) == 1

    @pytest.mark.cwe(639)
    async def test_another_owner_reads_nothing(
        self,
        wallets: SqlaWalletRepository,
        ledger: SqlaLedgerRepository,
        session: AsyncSession,
    ) -> None:
        """Knowing the wallet id must not be enough.

        Ids travel — in logs, in support tickets, in a screenshot. The join to
        the owning wallet is what makes possession of an id worthless.
        """
        owner = OwnerId(uuid4())
        wallet = await seed_wallet(wallets, owner)
        await ledger.add(entry(wallet, "10.00"))
        await session.commit()

        page = await ledger.list_by_wallet(wallet.id_, OwnerId(uuid4()), limit=10)

        assert page.items == []

    @pytest.mark.cwe(639)
    async def test_a_single_entry_is_scoped_too(
        self,
        wallets: SqlaWalletRepository,
        ledger: SqlaLedgerRepository,
        session: AsyncSession,
    ) -> None:
        owner = OwnerId(uuid4())
        wallet = await seed_wallet(wallets, owner)
        recorded = entry(wallet, "10.00")
        await ledger.add(recorded)
        await session.commit()

        assert await ledger.read(recorded.id_, owner) is not None
        assert await ledger.read(recorded.id_, OwnerId(uuid4())) is None


class TestLedgerPagination:
    async def test_pages_cover_every_entry_exactly_once(
        self,
        wallets: SqlaWalletRepository,
        ledger: SqlaLedgerRepository,
        session: AsyncSession,
    ) -> None:
        owner = OwnerId(uuid4())
        wallet = await seed_wallet(wallets, owner)
        created = [entry(wallet, "1.00") for _ in range(5)]
        for item in created:
            await ledger.add(item)
        await session.commit()

        seen: list[LedgerEntryId] = []
        cursor: str | None = None
        for _ in range(10):
            page = await ledger.list_by_wallet(wallet.id_, owner, limit=2, cursor=cursor)
            seen.extend(item.id_ for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert cursor is None
        assert set(seen) == {item.id_ for item in created}
        assert len(seen) == 5

    async def test_entries_come_back_newest_first(
        self,
        wallets: SqlaWalletRepository,
        ledger: SqlaLedgerRepository,
        session: AsyncSession,
    ) -> None:
        """UUIDv7 is time-ordered, which is what makes `ORDER BY id DESC` mean
        `newest first` without a second sort column."""
        owner = OwnerId(uuid4())
        wallet = await seed_wallet(wallets, owner)
        created = [entry(wallet, "1.00") for _ in range(3)]
        for item in created:
            await ledger.add(item)
        await session.commit()

        page = await ledger.list_by_wallet(wallet.id_, owner, limit=10)

        assert [item.id_ for item in page.items] == sorted(
            (item.id_ for item in created), key=lambda i: i.value, reverse=True
        )
