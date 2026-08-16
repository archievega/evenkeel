import time
from dataclasses import dataclass

from evenkeel.application.ports import (
    Clock,
    IdGenerator,
    MetricsPort,
    TransactionManager,
    WalletRepository,
)
from evenkeel.domain.entities.wallet import Wallet
from evenkeel.domain.value_objects.ids import OwnerId, WalletId
from evenkeel.domain.value_objects.money import CurrencyCode


@dataclass(frozen=True, slots=True)
class OpenWalletCommand:
    owner_id: OwnerId
    currency: CurrencyCode


@dataclass(frozen=True, slots=True)
class OpenWalletResult:
    wallet_id: WalletId
    currency: CurrencyCode


class OpenWalletInteractor:
    def __init__(
        self,
        wallets: WalletRepository,
        transaction_manager: TransactionManager,
        clock: Clock,
        id_generator: IdGenerator,
        metrics: MetricsPort,
    ) -> None:
        self._wallets = wallets
        self._transaction_manager = transaction_manager
        self._clock = clock
        self._ids = id_generator
        self._metrics = metrics

    async def __call__(self, command: OpenWalletCommand) -> OpenWalletResult:
        started_at = time.perf_counter()
        outcome = "error"
        try:
            wallet = Wallet.open(
                id_=WalletId(self._ids.generate()),
                owner_id=command.owner_id,
                currency=command.currency,
                now=self._clock.now(),
            )
            await self._wallets.add(wallet)
            await self._transaction_manager.commit()
            outcome = "created"
            return OpenWalletResult(wallet_id=wallet.id_, currency=wallet.currency)
        finally:
            self._metrics.observe_interactor(
                name="open_wallet",
                outcome=outcome,
                duration_seconds=time.perf_counter() - started_at,
            )
