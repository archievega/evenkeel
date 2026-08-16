from dataclasses import dataclass

from appcore.domain.base.identifier import Identifier


@dataclass(frozen=True, slots=True, repr=False)
class WalletId(Identifier):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class OwnerId(Identifier):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class LedgerEntryId(Identifier):
    pass
