"""The transaction contract of the request-scoped session.

A rule that circulates in the FastAPI community says: do not use
`async_sessionmaker` as a context manager for transaction management, because it
only closes and rolls back — it never commits. That is correct, and it is
exactly why this template commits explicitly through `TransactionManager`.

The half of that statement worth pinning down is the rollback. The DI provider
yields the session from `async with session_factory()`, so an interactor that
raises after writing must leave nothing behind. Asserting it here means the
guarantee survives a future refactor of the provider.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evenkeel.domain.entities.wallet import Wallet
from evenkeel.domain.value_objects.ids import OwnerId, WalletId
from evenkeel.domain.value_objects.money import CurrencyCode, Money
from evenkeel.infrastructure.adapters.sqla.wallet_repository import SqlaWalletRepository
from tests.integration.test_wallet_repository import EUR, NOW

pytestmark = pytest.mark.integration


async def test_an_uncommitted_write_does_not_survive_the_session(
    session_factory: async_sessionmaker[AsyncSession], clean_tables: None
) -> None:
    owner = OwnerId(uuid4())
    wallet = Wallet.open(id_=WalletId(uuid4()), owner_id=owner, currency=EUR, now=NOW)

    # Same shape as the DI provider: `async with session_factory()`, no commit.
    async with session_factory() as session:
        await SqlaWalletRepository(session).add(wallet)

    async with session_factory() as check:
        assert await SqlaWalletRepository(check).read(wallet.id_, owner) is None


async def test_a_write_abandoned_by_an_exception_does_not_survive(
    session_factory: async_sessionmaker[AsyncSession], clean_tables: None
) -> None:
    """The interactor path: write, then fail before the commit."""
    owner = OwnerId(uuid4())
    wallet = Wallet.open(id_=WalletId(uuid4()), owner_id=owner, currency=EUR, now=NOW)

    with pytest.raises(RuntimeError):
        async with session_factory() as session:
            await SqlaWalletRepository(session).add(wallet)
            raise RuntimeError("interactor failed after writing")

    async with session_factory() as check:
        assert await SqlaWalletRepository(check).read(wallet.id_, owner) is None


async def test_a_committed_write_does_survive(
    session_factory: async_sessionmaker[AsyncSession], clean_tables: None
) -> None:
    """The control case, so the two tests above cannot pass for the wrong reason."""
    owner = OwnerId(uuid4())
    wallet = Wallet.open(id_=WalletId(uuid4()), owner_id=owner, currency=EUR, now=NOW)
    wallet.deposit(Money(amount=Decimal("5.00"), currency=CurrencyCode("EUR")), now=NOW)

    async with session_factory() as session:
        await SqlaWalletRepository(session).add(wallet)
        await session.commit()

    async with session_factory() as check:
        stored = await SqlaWalletRepository(check).read(wallet.id_, owner)
        assert stored is not None
        assert stored.balance.amount == Decimal("5.00")
