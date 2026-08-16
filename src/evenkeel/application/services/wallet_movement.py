import hashlib
import json
import time
from dataclasses import dataclass
from uuid import UUID

from evenkeel.application.errors import (
    ApplicationErrorCode,
    ConflictError,
    NotFoundError,
)
from evenkeel.application.ports import (
    Clock,
    DistributedLockPort,
    IdempotencyRecord,
    IdempotencyStore,
    IdGenerator,
    LedgerRepository,
    MetricsPort,
    TransactionManager,
    WalletRepository,
)
from evenkeel.domain.entities.ledger_entry import LedgerDirection, LedgerEntry
from evenkeel.domain.entities.wallet import Wallet
from evenkeel.domain.value_objects.ids import LedgerEntryId, OwnerId, WalletId
from evenkeel.domain.value_objects.money import Money


@dataclass(frozen=True, slots=True)
class WalletMovementSettings:
    lock_ttl_ms: int = 5_000
    lock_wait_ms: int = 2_000
    idempotency_ttl_seconds: int = 24 * 3600


@dataclass(frozen=True, slots=True)
class MovementOutcome:
    wallet: Wallet
    entry: LedgerEntry
    replayed: bool


@dataclass(frozen=True, slots=True)
class MovementRequest:
    wallet_id: WalletId
    owner_id: OwnerId
    amount: Money
    direction: LedgerDirection
    description: str
    idempotency_key: str | None


class WalletMovementService:
    """The one place where a balance changes.

    Deposit and withdraw differ only in which domain method runs, so the
    concurrency, idempotency and persistence rules live here once. Three
    independent guards stack, because each covers what the others cannot:

    * the idempotency key stops a client retry from applying twice;
    * the distributed lock serialises concurrent writers on one wallet, so the
      slow path (load, decide, write) is not interleaved;
    * the version check is the last line of defence and is the only one that
      still holds if the lock backend is unavailable or a writer bypasses it.
    """

    def __init__(
        self,
        wallets: WalletRepository,
        ledger: LedgerRepository,
        transaction_manager: TransactionManager,
        distributed_lock: DistributedLockPort,
        idempotency: IdempotencyStore,
        clock: Clock,
        id_generator: IdGenerator,
        metrics: MetricsPort,
        settings: WalletMovementSettings,
    ) -> None:
        self._wallets = wallets
        self._ledger = ledger
        self._transaction_manager = transaction_manager
        self._lock = distributed_lock
        self._idempotency = idempotency
        self._clock = clock
        self._ids = id_generator
        self._metrics = metrics
        self._settings = settings

    async def apply(self, request: MovementRequest) -> MovementOutcome:
        replay = await self._replayed(request)
        if replay is not None:
            return replay

        started_at = time.perf_counter()
        async with self._lock.lock(
            f"wallet:{request.wallet_id.value}",
            ttl_ms=self._settings.lock_ttl_ms,
            wait_timeout_ms=self._settings.lock_wait_ms,
        ) as lock:
            self._metrics.observe_lock(
                name="wallet_movement",
                outcome="acquired" if lock.acquired else "timeout",
                wait_seconds=time.perf_counter() - started_at,
            )
            if not lock.acquired:
                await self._transaction_manager.rollback()
                raise ConflictError(
                    ApplicationErrorCode.WALLET_BUSY,
                    details={"wallet_id": str(request.wallet_id.value)},
                )
            return await self._apply_locked(request)

    async def _apply_locked(self, request: MovementRequest) -> MovementOutcome:
        # Ownership is part of the query, so there is no separate check to
        # forget here, and a wallet belonging to someone else is simply absent.
        wallet = await self._wallets.read(
            request.wallet_id, request.owner_id, for_update=True
        )
        if wallet is None:
            await self._transaction_manager.rollback()
            raise NotFoundError(
                ApplicationErrorCode.WALLET_NOT_FOUND,
                details={"wallet_id": str(request.wallet_id.value)},
            )

        now = self._clock.now()
        expected_version = wallet.version
        if request.direction is LedgerDirection.CREDIT:
            balance_after = wallet.deposit(request.amount, now=now)
        else:
            balance_after = wallet.withdraw(request.amount, now=now)

        entry = LedgerEntry.record(
            id_=LedgerEntryId(self._ids.generate()),
            wallet_id=wallet.id_,
            direction=request.direction,
            amount=request.amount,
            balance_after=balance_after,
            description=request.description,
            now=now,
        )

        await self._ledger.add(entry)
        updated = await self._wallets.update(wallet, expected_version=expected_version)
        if not updated:
            await self._transaction_manager.rollback()
            raise ConflictError(
                ApplicationErrorCode.WALLET_VERSION_CONFLICT,
                details={"wallet_id": str(wallet.id_.value)},
            )

        await self._transaction_manager.commit()
        await self._remember(request, wallet, entry)
        return MovementOutcome(wallet=wallet, entry=entry, replayed=False)

    async def _replayed(self, request: MovementRequest) -> MovementOutcome | None:
        if request.idempotency_key is None:
            return None
        record = await self._idempotency.get(request.idempotency_key)
        if record is None:
            return None
        if record.fingerprint != self._fingerprint(request):
            raise ConflictError(
                ApplicationErrorCode.IDEMPOTENCY_KEY_REUSED,
                details={"idempotency_key": request.idempotency_key},
            )
        entry_id = LedgerEntryId(UUID(str(record.response["entry_id"])))
        entry = await self._ledger.read(entry_id, request.owner_id)
        wallet = await self._wallets.read(request.wallet_id, request.owner_id)
        if entry is None or wallet is None:
            # The stored key outlived its rows (restore from backup, manual
            # cleanup). Falling through and re-applying is the lesser evil
            # compared with returning a receipt for data that no longer exists.
            return None
        await self._transaction_manager.rollback()
        return MovementOutcome(wallet=wallet, entry=entry, replayed=True)

    async def _remember(
        self, request: MovementRequest, wallet: Wallet, entry: LedgerEntry
    ) -> None:
        if request.idempotency_key is None:
            return
        await self._idempotency.put(
            IdempotencyRecord(
                key=request.idempotency_key,
                fingerprint=self._fingerprint(request),
                response={
                    "entry_id": str(entry.id_.value),
                    "wallet_id": str(wallet.id_.value),
                },
            ),
            ttl_seconds=self._settings.idempotency_ttl_seconds,
        )

    @staticmethod
    def _fingerprint(request: MovementRequest) -> str:
        payload = json.dumps(
            {
                "wallet_id": str(request.wallet_id.value),
                "owner_id": str(request.owner_id.value),
                "amount": str(request.amount.amount),
                "currency": request.amount.currency.value,
                "direction": request.direction.value,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "MovementOutcome",
    "MovementRequest",
    "WalletMovementService",
    "WalletMovementSettings",
]
