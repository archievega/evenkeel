from dataclasses import dataclass
from typing import Protocol

from appcore.domain.entities.ledger_entry import LedgerEntry
from appcore.domain.entities.wallet import Wallet
from appcore.domain.value_objects.ids import LedgerEntryId, OwnerId, WalletId


@dataclass(frozen=True, slots=True)
class Page[ItemT]:
    """One slice of a cursor-paginated result.

    Cursor rather than offset: ``LIMIT ... OFFSET n`` makes the database scan
    and discard n rows, so deep pages get linearly slower, and concurrent
    inserts shift rows between pages so clients silently skip or repeat items.
    """

    items: list[ItemT]
    next_cursor: str | None


class WalletRepository(Protocol):
    """Ownership is a parameter, not a follow-up check.

    Every read takes the ``owner_id`` it is being performed on behalf of and
    filters in SQL, so a query that could return another owner's row is not
    something you can express. The alternative -- load by id, then remember to
    compare owners in the use case -- is the single most common source of IDOR:
    it works everywhere it is written, and the vulnerability is the one place
    someone forgot.

    A row that exists but belongs to someone else is reported as absent. Telling
    an attacker "this id exists but is not yours" is an existence oracle for
    free.
    """

    async def read(
        self,
        wallet_id: WalletId,
        owner_id: OwnerId,
        *,
        for_update: bool = False,
    ) -> Wallet | None:
        """Load an owned wallet. ``for_update`` takes a row lock for writes."""
        ...

    async def list_by_owner(
        self,
        owner_id: OwnerId,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> Page[Wallet]: ...

    async def add(self, wallet: Wallet) -> None: ...

    async def update(self, wallet: Wallet, *, expected_version: int) -> bool:
        """Persist with an optimistic-concurrency guard.

        Returns ``False`` when no row matched ``expected_version``, meaning a
        concurrent writer already advanced the aggregate and the caller must
        retry or fail rather than overwrite.
        """
        ...


class LedgerRepository(Protocol):
    async def add(self, entry: LedgerEntry) -> None: ...

    async def read(
        self, entry_id: LedgerEntryId, owner_id: OwnerId
    ) -> LedgerEntry | None:
        """Entries are scoped through their wallet's owner, same rule as above."""
        ...

    async def list_by_wallet(
        self,
        wallet_id: WalletId,
        owner_id: OwnerId,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> Page[LedgerEntry]: ...
