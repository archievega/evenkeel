from evenkeel.application.ports import Page
from evenkeel.domain.entities.ledger_entry import LedgerEntry
from evenkeel.domain.entities.wallet import Wallet
from evenkeel.domain.value_objects.ids import LedgerEntryId, OwnerId, WalletId


class FakeTransactionManager:
    """Records the commit/rollback decisions a test wants to assert on."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeWalletRepository:
    """In-memory wallets that enforce the same optimistic-concurrency contract.

    A fake that always accepts a write cannot fail the way the real adapter
    fails, so the version check would be tested nowhere. Fakes must keep the
    contract; the shared contract suite is what proves they do.
    """

    def __init__(self, wallets: list[Wallet] | None = None) -> None:
        self._wallets: dict[WalletId, Wallet] = {w.id_: w for w in wallets or []}
        self._versions: dict[WalletId, int] = {w.id_: w.version for w in wallets or []}
        self.for_update_calls = 0

    async def read(
        self, wallet_id: WalletId, owner_id: OwnerId, *, for_update: bool = False
    ) -> Wallet | None:
        if for_update:
            self.for_update_calls += 1
        wallet = self._wallets.get(wallet_id)
        # The fake applies the same scoping the SQL does. A fake that ignored
        # owner_id would make every ownership test pass for the wrong reason.
        if wallet is None or wallet.owner_id != owner_id:
            return None
        return wallet

    async def list_by_owner(
        self, owner_id: OwnerId, *, limit: int, cursor: str | None = None
    ) -> Page[Wallet]:
        # Newest first, like `ORDER BY id DESC` in the SQL adapter. Ids are
        # UUIDv7, so id order is time order. A fake that returns insertion
        # order passes tests the real repository would fail.
        items = sorted(
            (w for w in self._wallets.values() if w.owner_id == owner_id),
            key=lambda w: w.id_.value,
            reverse=True,
        )
        return Page(items=items[:limit], next_cursor=None)

    async def add(self, wallet: Wallet) -> None:
        self._wallets[wallet.id_] = wallet
        self._versions[wallet.id_] = wallet.version

    async def update(self, wallet: Wallet, *, expected_version: int) -> bool:
        if self._versions.get(wallet.id_) != expected_version:
            return False
        self._wallets[wallet.id_] = wallet
        self._versions[wallet.id_] = wallet.version
        return True

    def force_version(self, wallet_id: WalletId, version: int) -> None:
        """Simulate a concurrent writer having advanced the row."""
        self._versions[wallet_id] = version


class FakeLedgerRepository:
    def __init__(self) -> None:
        self.entries: dict[LedgerEntryId, LedgerEntry] = {}

    async def add(self, entry: LedgerEntry) -> None:
        self.entries[entry.id_] = entry

    async def read(
        self, entry_id: LedgerEntryId, owner_id: OwnerId
    ) -> LedgerEntry | None:
        return self.entries.get(entry_id)

    async def list_by_wallet(
        self,
        wallet_id: WalletId,
        owner_id: OwnerId,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> Page[LedgerEntry]:
        items = sorted(
            (e for e in self.entries.values() if e.wallet_id == wallet_id),
            key=lambda e: e.id_.value,
            reverse=True,
        )
        return Page(items=items[:limit], next_cursor=None)
