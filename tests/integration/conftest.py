"""Integration fixtures: a real PostgreSQL, schema built by the real migrations.

Two ways to get a database, in priority order:

1. ``TEST_DATABASE_URL`` — use an existing server (CI service container, the
   local compose stack). Fast, and the same escape hatch the Redis contract
   suite uses.
2. testcontainers — start a throwaway PostgreSQL. Hermetic, needs Docker.

The schema is created by running ``alembic upgrade head``, never by
``metadata.create_all``. Those two can disagree, and when they do, the tests
pass against a schema that production never has: the migration is the artefact
that actually ships.
"""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from evenkeel.infrastructure.adapters.sqla.ledger_repository import SqlaLedgerRepository
from evenkeel.infrastructure.adapters.sqla.wallet_repository import SqlaWalletRepository

pytestmark = pytest.mark.integration

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    provided = os.getenv("TEST_DATABASE_URL")
    if provided:
        yield provided
        return

    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - depends on the installed extras
        pytest.skip("testcontainers is not installed and TEST_DATABASE_URL is unset")

    with PostgresContainer("postgres:17.4-alpine", driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def _migrated(database_url: str) -> None:
    config = Config(os.path.join(ROOT, "alembic.ini"))
    config.set_main_option(
        "script_location", os.path.join(ROOT, "src/evenkeel/infrastructure/sqla/alembic")
    )
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


@pytest.fixture
async def engine(database_url: str, _migrated: None) -> AsyncIterator[AsyncEngine]:
    """Per-test engine, deliberately not shared across the session.

    asyncpg connections are bound to the event loop that created them, and
    pytest-asyncio gives each test its own loop. A session-scoped engine
    therefore hands a connection from one loop to another and fails with
    "another operation is in progress" — a confusing error that has nothing to
    do with the code under test. NullPool keeps nothing to reuse.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """Empty the tables before each test.

    Truncation rather than an outer transaction that is rolled back: several
    tests need genuine concurrent sessions with real commits to exercise row
    locks and the version check, and those cannot happen inside one shared
    transaction.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE ledger_entries, wallets RESTART IDENTITY CASCADE")
        )
    yield


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession], clean_tables: None
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def wallets(session: AsyncSession) -> SqlaWalletRepository:
    return SqlaWalletRepository(session)


@pytest.fixture
def ledger(session: AsyncSession) -> SqlaLedgerRepository:
    return SqlaLedgerRepository(session)
