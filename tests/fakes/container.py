"""The real object graph with the outermost adapters swapped for memory.

Lives here rather than in `tests/http/conftest.py` because it stopped being an
HTTP concern the moment a second transport appeared: the MCP tools resolve the
same interactors from the same container, and two copies of this wiring would
drift until one transport was quietly being tested against different rules than
the other.
"""

from dishka import (
    AsyncContainer,
    Provider,
    Scope,
    from_context,
    make_async_container,
    provide,
)
from dishka.integrations.fastapi import FastapiProvider

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
from evenkeel.infrastructure.adapters.dev_identity import DevIdentityProvider
from evenkeel.infrastructure.adapters.memory.idempotency import InMemoryIdempotencyStore
from evenkeel.infrastructure.adapters.memory.locking import (
    InMemoryDistributedLock,
    InMemoryRateLimiter,
)
from evenkeel.infrastructure.adapters.noop.metrics import NoopMetrics
from evenkeel.infrastructure.adapters.system import Uuid7Generator
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
        # The production generator, not `uuid4`. Ids are the sort key of every
        # cursor-paginated read — `ORDER BY id DESC` is time order only because
        # UUIDv7 is time-ordered — so a random generator here makes the fakes
        # return a different order than the database and hides it.
        return Uuid7Generator()

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


def build_container(
    wallets: FakeWalletRepository,
    ledger: FakeLedgerRepository,
    risk: ScriptedRiskAssessment,
) -> AsyncContainer:
    return make_async_container(
        FakeInfrastructureProvider(),
        FakeRequestProvider(),
        FastapiProvider(),
        context={
            FakeWalletRepository: wallets,
            FakeLedgerRepository: ledger,
            RiskAssessmentPort: risk,
        },
    )
