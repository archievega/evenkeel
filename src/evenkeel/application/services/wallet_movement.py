import hashlib
import json
import time
from dataclasses import dataclass
from uuid import UUID

from evenkeel.application.errors import (
    ApplicationErrorCode,
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
)
from evenkeel.application.ports import (
    Clock,
    DistributedLockPort,
    IdempotencyOutcome,
    IdempotencyStore,
    IdGenerator,
    LedgerRepository,
    MetricsPort,
    TransactionManager,
    WalletRepository,
)
from evenkeel.domain.entities.ledger_entry import LedgerDirection, LedgerEntry
from evenkeel.domain.entities.wallet import Wallet
from evenkeel.domain.errors import DomainError
from evenkeel.domain.value_objects.ids import LedgerEntryId, OwnerId, WalletId
from evenkeel.domain.value_objects.money import Money
from evenkeel.logging import get_logger

log = get_logger(__name__)


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
    # Carried separately from `wallet.balance`, which is whatever the wallet
    # holds *now*. On a replay those differ: the entry is the original, and the
    # wallet has moved on. Returning them side by side gave one response that
    # disagreed with itself and contradicted ADR 4's promise of "the original
    # result". A receipt reports the balance it was written with.
    balance: Money


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
        # Claimed before anything happens, and confirmed after the commit.
        #
        # The previous shape wrote the record *after* committing, which left a
        # window: a store that was unreachable in that moment returned 503 for
        # money that had already moved, with nothing recorded, so the client's
        # retry moved it again.
        #
        # It also needed the check in two places — once outside the lock for
        # the common retry and once inside it for the genuine race — and the
        # first attempt at that combination answered 409 for a movement that
        # had completed. An atomic claim needs neither: two concurrent requests
        # with one key cannot both be `RESERVED`.
        replay = await self._claim(request)
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
                await self._release(request)
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
            await self._release(request)
            raise NotFoundError(
                ApplicationErrorCode.WALLET_NOT_FOUND,
                details={"wallet_id": str(request.wallet_id.value)},
            )

        now = self._clock.now()
        expected_version = wallet.version
        try:
            if request.direction is LedgerDirection.CREDIT:
                balance_after = wallet.deposit(request.amount, now=now)
            else:
                balance_after = wallet.withdraw(request.amount, now=now)
        except DomainError:
            # Insufficient funds is a refusal, not an exception to leak upward
            # with an open transaction. Every other branch here rolls back; a
            # session handed on mid-transaction is the next request's problem.
            await self._transaction_manager.rollback()
            # And the key goes back: nothing happened, so a retry with it should
            # be allowed to reach a different answer once the wallet is funded.
            await self._release(request)
            raise

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
            await self._release(request)
            raise ConflictError(
                ApplicationErrorCode.WALLET_VERSION_CONFLICT,
                details={"wallet_id": str(wallet.id_.value)},
            )

        await self._transaction_manager.commit()
        await self._confirm(request, wallet, entry)
        return MovementOutcome(
            wallet=wallet, entry=entry, replayed=False, balance=wallet.balance
        )

    @staticmethod
    def _scoped_key(request: MovementRequest) -> str:
        """The client's key, namespaced by owner.

        The key is a string the caller invents. `k-42`, `1`, `retry` and today's
        date all get chosen, by more than one caller, and the store is one
        keyspace shared by every tenant. Unscoped, the second owner to use `k-42`
        is told their key was "already used with a different payload" — a 409
        for an operation they never made, and an oracle telling them which keys
        somebody else is using.

        Found by recording the README demo twice with the same key.

        `owner_id` is also in the fingerprint. That is not redundant: the
        fingerprint decides whether a *replay* is genuine, and this decides
        whose keyspace is being read at all.
        """
        return f"{request.owner_id.value}:{request.idempotency_key}"

    async def _claim(self, request: MovementRequest) -> MovementOutcome | None:
        """Take the key, or explain what already has it."""
        if request.idempotency_key is None:
            return None

        reservation = await self._idempotency.reserve(
            self._scoped_key(request),
            fingerprint=self._fingerprint(request),
            ttl_seconds=self._settings.idempotency_ttl_seconds,
        )
        if reservation.may_proceed:
            return None

        if reservation.outcome is IdempotencyOutcome.CONFLICT:
            raise ConflictError(
                ApplicationErrorCode.IDEMPOTENCY_KEY_REUSED,
                details={"idempotency_key": request.idempotency_key},
            )
        if reservation.outcome is IdempotencyOutcome.IN_PROGRESS:
            # The original is still running. Answering it as a replay would mean
            # inventing a result nobody has produced yet, so the honest answer
            # is "ask again".
            raise ConflictError(
                ApplicationErrorCode.MOVEMENT_IN_PROGRESS,
                details={"idempotency_key": request.idempotency_key},
            )

        record = reservation.record
        if record is None:
            return None
        entry_id = LedgerEntryId(UUID(str(record.response["entry_id"])))
        entry = await self._ledger.read(entry_id, request.owner_id)
        wallet = await self._wallets.read(request.wallet_id, request.owner_id)
        if entry is None or wallet is None:
            # The record outlived its rows (restore from backup, manual
            # cleanup). Falling through and re-applying is the lesser evil
            # against returning a receipt for data that no longer exists.
            return None
        await self._transaction_manager.rollback()
        return MovementOutcome(
            wallet=wallet, entry=entry, replayed=True, balance=entry.balance_after
        )

    async def _confirm(
        self, request: MovementRequest, wallet: Wallet, entry: LedgerEntry
    ) -> None:
        """Store the receipt, and never fail the caller for not managing to.

        This runs after the commit, so by the time it is reached the money has
        moved and the entry exists. Letting a store outage out of here answered
        `503` for a movement that had succeeded — and worse, the reservation
        stayed unconfirmed, so the retry it invited got `409 MOVEMENT_IN_PROGRESS`
        for the whole 24-hour key lifetime. The caller was told their movement
        failed, then prevented from doing it again.

        ADR 8 always described this window honestly; two other documents claimed
        it could not happen, and no test made `confirm` raise. Now one does.

        Swallowing it costs the receipt: a retry within the key's lifetime gets
        `409` rather than a replay of the original response. That is the safe
        direction — it never applies the movement twice — and the caller who
        made the request has their answer.
        """
        if request.idempotency_key is None:
            return
        started_at = time.perf_counter()
        outcome = "success"
        try:
            await self._idempotency.confirm(
                self._scoped_key(request),
                response={
                    "entry_id": str(entry.id_.value),
                    "wallet_id": str(wallet.id_.value),
                },
                ttl_seconds=self._settings.idempotency_ttl_seconds,
            )
        except DependencyUnavailableError:
            outcome = "unavailable"
            log.warning(
                "idempotency_confirm_failed",
                wallet_id=str(wallet.id_.value),
                entry_id=str(entry.id_.value),
            )
        finally:
            # On the existing outbound counter rather than a new port method:
            # the idempotency store is a dependency like any other, and one
            # counter is not worth a port, two adapters, a conformance entry
            # and a dashboard row.
            self._metrics.observe_external_call(
                service="idempotency",
                operation="confirm",
                outcome=outcome,
                duration_seconds=time.perf_counter() - started_at,
            )

    async def _release(self, request: MovementRequest) -> None:
        if request.idempotency_key is None:
            return
        await self._idempotency.release(self._scoped_key(request))

    @staticmethod
    def _fingerprint(request: MovementRequest) -> str:
        payload = json.dumps(
            {
                "wallet_id": str(request.wallet_id.value),
                "owner_id": str(request.owner_id.value),
                "amount": str(request.amount.amount),
                "currency": request.amount.currency.value,
                "direction": request.direction.value,
                # Included, or the same key with a different description
                # replays silently and returns a receipt for an operation the
                # caller never made.
                "description": request.description,
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
