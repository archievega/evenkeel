import time
from dataclasses import dataclass

from appcore.application.errors import (
    ApplicationErrorCode,
    NotFoundError,
    ValidationError,
)
from appcore.application.ports import (
    LedgerRepository,
    MetricsPort,
    Page,
    WalletRepository,
)
from appcore.domain.entities.ledger_entry import LedgerEntry
from appcore.domain.entities.wallet import Wallet
from appcore.domain.value_objects.ids import OwnerId, WalletId

MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class GetWalletQuery:
    wallet_id: WalletId
    owner_id: OwnerId


@dataclass(frozen=True, slots=True)
class ListWalletsQuery:
    owner_id: OwnerId
    limit: int = 20
    cursor: str | None = None

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > MAX_PAGE_SIZE:
            raise ValidationError(
                ApplicationErrorCode.VALIDATION_FAILED,
                details={"limit": f"must be between 1 and {MAX_PAGE_SIZE}"},
            )


@dataclass(frozen=True, slots=True)
class ListLedgerQuery:
    wallet_id: WalletId
    owner_id: OwnerId
    limit: int = 20
    cursor: str | None = None

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > MAX_PAGE_SIZE:
            raise ValidationError(
                ApplicationErrorCode.VALIDATION_FAILED,
                details={"limit": f"must be between 1 and {MAX_PAGE_SIZE}"},
            )


class GetWalletInteractor:
    def __init__(self, wallets: WalletRepository, metrics: MetricsPort) -> None:
        self._wallets = wallets
        self._metrics = metrics

    async def __call__(self, query: GetWalletQuery) -> Wallet:
        started_at = time.perf_counter()
        outcome = "error"
        try:
            wallet = await self._wallets.read(query.wallet_id, query.owner_id)
            if wallet is None:
                outcome = "not_found"
                raise NotFoundError(
                    ApplicationErrorCode.WALLET_NOT_FOUND,
                    details={"wallet_id": str(query.wallet_id.value)},
                )
            outcome = "success"
            return wallet
        finally:
            self._metrics.observe_interactor(
                name="get_wallet",
                outcome=outcome,
                duration_seconds=time.perf_counter() - started_at,
            )


class ListWalletsInteractor:
    def __init__(self, wallets: WalletRepository, metrics: MetricsPort) -> None:
        self._wallets = wallets
        self._metrics = metrics

    async def __call__(self, query: ListWalletsQuery) -> Page[Wallet]:
        started_at = time.perf_counter()
        try:
            return await self._wallets.list_by_owner(
                query.owner_id, limit=query.limit, cursor=query.cursor
            )
        finally:
            self._metrics.observe_interactor(
                name="list_wallets",
                outcome="success",
                duration_seconds=time.perf_counter() - started_at,
            )


class ListLedgerEntriesInteractor:
    """Reads are scoped exactly like writes.

    Read endpoints are where authorization is most often skipped, because
    "it only returns data" feels harmless right up until it returns someone
    else's. Passing ``owner_id`` into the query makes skipping it impossible.
    """

    def __init__(
        self,
        wallets: WalletRepository,
        ledger: LedgerRepository,
        metrics: MetricsPort,
    ) -> None:
        self._wallets = wallets
        self._ledger = ledger
        self._metrics = metrics

    async def __call__(self, query: ListLedgerQuery) -> Page[LedgerEntry]:
        started_at = time.perf_counter()
        outcome = "error"
        try:
            wallet = await self._wallets.read(query.wallet_id, query.owner_id)
            if wallet is None:
                outcome = "not_found"
                raise NotFoundError(
                    ApplicationErrorCode.WALLET_NOT_FOUND,
                    details={"wallet_id": str(query.wallet_id.value)},
                )
            page = await self._ledger.list_by_wallet(
                query.wallet_id, query.owner_id, limit=query.limit, cursor=query.cursor
            )
            outcome = "success"
            return page
        finally:
            self._metrics.observe_interactor(
                name="list_ledger_entries",
                outcome=outcome,
                duration_seconds=time.perf_counter() - started_at,
            )
