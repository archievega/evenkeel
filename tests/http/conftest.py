from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from dishka import Provider, Scope, from_context, make_async_container, provide
from dishka.integrations.fastapi import FastapiProvider
from httpx import ASGITransport, AsyncClient

from evenkeel.application.interactors.wallets import (
    DepositToWalletInteractor,
    GetWalletInteractor,
    ListLedgerEntriesInteractor,
    ListWalletsInteractor,
    MoveMoneySettings,
    OpenWalletInteractor,
    WithdrawFromWalletInteractor,
)
from evenkeel.application.ports import (
    Clock,
    DistributedLockPort,
    IdempotencyStore,
    IdGenerator,
    LedgerRepository,
    MetricsPort,
    RateLimiterPort,
    RiskAssessmentPort,
    TransactionManager,
    WalletRepository,
)
from evenkeel.application.ports.identity import IdentityProvider
from evenkeel.application.services.wallet_movement import (
    WalletMovementService,
    WalletMovementSettings,
)
from evenkeel.domain.value_objects.ids import OwnerId
from evenkeel.infrastructure.adapters.dev_identity import DevIdentityProvider
from evenkeel.infrastructure.adapters.memory.idempotency import InMemoryIdempotencyStore
from evenkeel.infrastructure.adapters.memory.locking import (
    InMemoryDistributedLock,
    InMemoryRateLimiter,
)
from evenkeel.infrastructure.adapters.noop.metrics import NoopMetrics
from evenkeel.setup.app_factory import create_app
from evenkeel.setup.config import Settings
from tests.fakes.repositories import (
    FakeLedgerRepository,
    FakeTransactionManager,
    FakeWalletRepository,
)
from tests.fakes.system import FixedClock, ScriptedRiskAssessment


class FakeInfrastructureProvider(Provider):
    """The real object graph with the database swapped for memory.

    The app under test is the one that ships: same routers, same middleware,
    same error handlers, same interactors. Only the outermost adapters differ,
    which is exactly the seam ports exist to provide.
    """

    scope = Scope.APP

    wallets = from_context(provides=FakeWalletRepository)
    ledger = from_context(provides=FakeLedgerRepository)

    @provide
    def metrics(self) -> MetricsPort:
        return NoopMetrics()

    @provide
    def clock(self) -> Clock:
        return FixedClock()

    @provide
    def ids(self) -> IdGenerator:
        return _RandomIds()

    @provide
    def identity(self) -> IdentityProvider:
        return DevIdentityProvider()

    @provide
    def lock(self) -> DistributedLockPort:
        return InMemoryDistributedLock()

    @provide
    def limiter(self) -> RateLimiterPort:
        return InMemoryRateLimiter()

    @provide
    def idempotency(self) -> IdempotencyStore:
        return InMemoryIdempotencyStore()

    risk = from_context(provides=RiskAssessmentPort)

    @provide
    def movement_settings(self) -> WalletMovementSettings:
        return WalletMovementSettings()

    @provide
    def move_money_settings(self) -> MoveMoneySettings:
        return MoveMoneySettings()


class FakeRequestProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def transaction_manager(self) -> TransactionManager:
        return FakeTransactionManager()

    @provide
    def wallet_repository(self, repo: FakeWalletRepository) -> WalletRepository:
        return repo

    @provide
    def ledger_repository(self, repo: FakeLedgerRepository) -> LedgerRepository:
        return repo

    movement = provide(WalletMovementService)
    open_wallet = provide(OpenWalletInteractor)
    deposit = provide(DepositToWalletInteractor)
    withdraw = provide(WithdrawFromWalletInteractor)
    get_wallet = provide(GetWalletInteractor)
    list_wallets = provide(ListWalletsInteractor)
    list_entries = provide(ListLedgerEntriesInteractor)


class _RandomIds:
    def generate(self) -> object:
        return uuid4()


@pytest.fixture
def wallets() -> FakeWalletRepository:
    return FakeWalletRepository()


@pytest.fixture
def ledger() -> FakeLedgerRepository:
    return FakeLedgerRepository()


@pytest.fixture
def risk() -> ScriptedRiskAssessment:
    """Allows everything unless a test replaces the decision."""
    return ScriptedRiskAssessment()


@pytest.fixture
def owner_id() -> OwnerId:
    return OwnerId(uuid4())


@pytest.fixture
async def client(
    wallets: FakeWalletRepository,
    ledger: FakeLedgerRepository,
    risk: ScriptedRiskAssessment,
) -> AsyncIterator[AsyncClient]:
    container = make_async_container(
        FakeInfrastructureProvider(),
        FakeRequestProvider(),
        FastapiProvider(),
        context={
            FakeWalletRepository: wallets,
            FakeLedgerRepository: ledger,
            RiskAssessmentPort: risk,
        },
    )
    app = create_app(settings=Settings(), container=container)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http_client,
    ):
        yield http_client
