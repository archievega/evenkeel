"""What the fakes cannot prove.

Every assertion here is about behaviour that lives in PostgreSQL or in the SQL
the adapter emits: row locks, the version predicate, NUMERIC arithmetic, the
timezone type decorator, CHECK constraints, and cursor ordering. An in-memory
fake satisfies all of these by construction, which is exactly why a suite built
only on fakes can be green while the adapter is broken.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from appcore.domain.entities.wallet import Wallet
from appcore.domain.value_objects.ids import OwnerId, WalletId
from appcore.domain.value_objects.money import CurrencyCode, Money
from appcore.infrastructure.adapters.sqla.wallet_repository import SqlaWalletRepository

pytestmark = pytest.mark.integration

EUR = CurrencyCode("EUR")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_wallet(owner_id: OwnerId, balance: str = "0.00") -> Wallet:
    wallet = Wallet.open(id_=WalletId(uuid4()), owner_id=owner_id, currency=EUR, now=NOW)
    if balance != "0.00":
        wallet.deposit(Money(amount=Decimal(balance), currency=EUR), now=NOW)
    return wallet


class TestOwnerScoping:
    async def test_the_owner_gets_their_wallet(
        self, wallets: SqlaWalletRepository, session: AsyncSession
    ) -> None:
        owner = OwnerId(uuid4())
        wallet = make_wallet(owner)
        await wallets.add(wallet)
        await session.commit()

        assert await wallets.read(wallet.id_, owner) is not None

    async def test_another_owner_gets_nothing(
        self, wallets: SqlaWalletRepository, session: AsyncSession
    ) -> None:
        """The security property, proven against the SQL that actually runs.

        The fake enforces this in Python. Here it must come from the WHERE
        clause, which is the only version that protects production.
        """
        owner = OwnerId(uuid4())
        wallet = make_wallet(owner)
        await wallets.add(wallet)
        await session.commit()

        assert await wallets.read(wallet.id_, OwnerId(uuid4())) is None

    async def test_listing_returns_only_the_owner_rows(
        self, wallets: SqlaWalletRepository, session: AsyncSession
    ) -> None:
        mine, theirs = OwnerId(uuid4()), OwnerId(uuid4())
        await wallets.add(make_wallet(mine))
        await wallets.add(make_wallet(theirs))
        await session.commit()

        page = await wallets.list_by_owner(mine, limit=50)

        assert [w.owner_id for w in page.items] == [mine]


class TestOptimisticConcurrency:
    async def test_a_stale_version_does_not_overwrite(
        self, wallets: SqlaWalletRepository, session: AsyncSession
    ) -> None:
        owner = OwnerId(uuid4())
        wallet = make_wallet(owner, "100.00")
        await wallets.add(wallet)
        await session.commit()

        wallet.deposit(Money(amount=Decimal("10.00"), currency=EUR), now=NOW)
        assert await wallets.update(wallet, expected_version=1) is True

        # A second writer that loaded the row before the update above still
        # holds version 1 and must lose rather than silently overwrite.
        assert await wallets.update(wallet, expected_version=1) is False

    async def test_two_concurrent_sessions_cannot_both_commit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clean_tables: None,
    ) -> None:
        """The lost update, with two real connections.

        Both read the same balance, both try to write it back. Exactly one
        UPDATE may match, or money is created out of nothing.
        """
        owner = OwnerId(uuid4())
        async with session_factory() as setup:
            await SqlaWalletRepository(setup).add(make_wallet(owner, "100.00"))
            await setup.commit()

        async with session_factory() as first, session_factory() as second:
            repo_a, repo_b = SqlaWalletRepository(first), SqlaWalletRepository(second)
            page = await repo_a.list_by_owner(owner, limit=1)
            wallet_id = page.items[0].id_

            a = await repo_a.read(wallet_id, owner)
            b = await repo_b.read(wallet_id, owner)
            assert a is not None and b is not None
            # Captured before mutating, exactly as WalletMovementService does:
            # the predicate must be the version that was read, not the one the
            # domain has already advanced.
            version_a, version_b = a.version, b.version
            assert version_a == version_b

            a.withdraw(Money(amount=Decimal("30.00"), currency=EUR), now=NOW)
            b.withdraw(Money(amount=Decimal("30.00"), currency=EUR), now=NOW)

            assert await repo_a.update(a, expected_version=version_a) is True
            await first.commit()

            assert await repo_b.update(b, expected_version=version_b) is False
            await second.rollback()

        async with session_factory() as check:
            final = await SqlaWalletRepository(check).read(wallet_id, owner)
            assert final is not None
            assert final.balance.amount == Decimal("70.00")


class TestRowLocking:
    async def test_for_update_blocks_a_second_reader(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clean_tables: None,
    ) -> None:
        """`for_update=True` must emit a real FOR UPDATE.

        Proven by asking the second transaction for a NOWAIT lock on the same
        row: if the first one did not lock it, NOWAIT succeeds and the test
        fails. A fake cannot express this at all.
        """
        owner = OwnerId(uuid4())
        async with session_factory() as setup:
            wallet = make_wallet(owner, "10.00")
            await SqlaWalletRepository(setup).add(wallet)
            await setup.commit()

        async with session_factory() as holder:
            locked = await SqlaWalletRepository(holder).read(
                wallet.id_, owner, for_update=True
            )
            assert locked is not None

            async with session_factory() as other:
                # DBAPIError, not a bare Exception: catching everything would
                # let an unrelated failure (a typo in the SQL, a dead
                # connection) pass as proof that the lock was taken.
                with pytest.raises(DBAPIError) as err:
                    await other.execute(
                        text(
                            "SELECT 1 FROM wallets WHERE id = :id FOR UPDATE NOWAIT"
                        ).bindparams(id=wallet.id_.value)
                    )
                assert "could not obtain lock" in str(err.value).lower()
                await other.rollback()

            await holder.rollback()


class TestDatabaseLevelInvariants:
    async def test_the_check_constraint_rejects_a_negative_balance(
        self, session: AsyncSession
    ) -> None:
        """The database refuses what the domain refuses.

        The application cannot be the only guard: a migration, a backfill or a
        psql session bypasses it entirely.
        """
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO wallets (id, owner_id, balance_amount, currency,"
                    " status, version, created_at, updated_at)"
                    " VALUES (:id, :owner, -1, 'EUR', 'open', 0, now(), now())"
                ).bindparams(id=uuid4(), owner=uuid4())
            )
        await session.rollback()


class TestTypeRoundTrips:
    async def test_money_survives_the_numeric_column(
        self, wallets: SqlaWalletRepository, session: AsyncSession
    ) -> None:
        owner = OwnerId(uuid4())
        wallet = make_wallet(owner, "0.10")
        await wallets.add(wallet)
        await session.commit()
        session.expunge_all()

        loaded = await wallets.read(wallet.id_, owner)

        assert loaded is not None
        assert loaded.balance.amount == Decimal("0.10")
        assert isinstance(loaded.balance.amount, Decimal)

    async def test_timestamps_come_back_timezone_aware(
        self, wallets: SqlaWalletRepository, session: AsyncSession
    ) -> None:
        """`TzAwareDateTime` is the single timezone boundary; this proves it holds.

        A naive datetime written by any other path must still be read back as
        aware, or the first comparison against an aware value raises TypeError
        in production.
        """
        owner = OwnerId(uuid4())
        wallet = make_wallet(owner)
        wallet.created_at = datetime(2026, 5, 1, 12, 0)  # naive on purpose
        await wallets.add(wallet)
        await session.commit()

        loaded = await wallets.read(wallet.id_, owner)

        assert loaded is not None
        assert loaded.created_at.tzinfo is not None
        assert loaded.created_at == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


class TestCursorPagination:
    async def test_pages_cover_every_row_exactly_once(
        self, wallets: SqlaWalletRepository, session: AsyncSession
    ) -> None:
        """Walk the whole set in pages and check for gaps and repeats.

        Off-by-one in a cursor shows up as a silently skipped or duplicated
        row, which no unit test with a single page would ever notice.
        """
        owner = OwnerId(uuid4())
        created = [make_wallet(owner) for _ in range(7)]
        for wallet in created:
            await wallets.add(wallet)
        await session.commit()

        seen: list[WalletId] = []
        cursor: str | None = None
        for _ in range(10):
            page = await wallets.list_by_owner(owner, limit=3, cursor=cursor)
            seen.extend(item.id_ for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert cursor is None
        assert len(seen) == 7
        assert len(set(seen)) == 7
        assert set(seen) == {w.id_ for w in created}

    async def test_the_last_page_reports_no_cursor(
        self, wallets: SqlaWalletRepository, session: AsyncSession
    ) -> None:
        owner = OwnerId(uuid4())
        for _ in range(3):
            await wallets.add(make_wallet(owner))
        await session.commit()

        page = await wallets.list_by_owner(owner, limit=10)

        assert len(page.items) == 3
        assert page.next_cursor is None
