from dishka import Provider, Scope, provide, provide_all

from evenkeel.application.interactors.wallets import (
    DepositToWalletInteractor,
    GetWalletInteractor,
    ListLedgerEntriesInteractor,
    ListWalletsInteractor,
    MoveMoneySettings,
    OpenWalletInteractor,
    WithdrawFromWalletInteractor,
)
from evenkeel.application.ports import LedgerRepository, WalletRepository
from evenkeel.application.services.wallet_movement import (
    WalletMovementService,
    WalletMovementSettings,
)
from evenkeel.infrastructure.adapters.sqla.ledger_repository import SqlaLedgerRepository
from evenkeel.infrastructure.adapters.sqla.wallet_repository import SqlaWalletRepository
from evenkeel.setup.config import MovementPolicyConfig


class WalletProvider(Provider):
    """Everything a wallet request needs, in one place.

    Grouping providers by bounded context rather than by technical kind keeps
    each file readable: adding a feature touches one provider module instead of
    appending to a single thousand-line registry.
    """

    scope = Scope.REQUEST

    wallet_repository = provide(SqlaWalletRepository, provides=WalletRepository)
    ledger_repository = provide(SqlaLedgerRepository, provides=LedgerRepository)
    movement_service = provide(WalletMovementService)

    interactors = provide_all(
        OpenWalletInteractor,
        DepositToWalletInteractor,
        WithdrawFromWalletInteractor,
        GetWalletInteractor,
        ListWalletsInteractor,
        ListLedgerEntriesInteractor,
    )

    @provide(scope=Scope.APP)
    def movement_settings(self) -> WalletMovementSettings:
        return WalletMovementSettings()

    @provide(scope=Scope.APP)
    def move_money_settings(self, config: MovementPolicyConfig) -> MoveMoneySettings:
        return MoveMoneySettings(
            rate_limit_enabled=config.rate_limit_enabled,
            rate_limit_limit=config.rate_limit_limit,
            rate_limit_window_seconds=config.rate_limit_window_seconds,
            risk_fail_open=config.risk_fail_open,
        )
