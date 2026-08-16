"""The one outbound call on the money path.

Unlike every other port here, this hides a service somebody else operates: it
can be slow rather than down, and answer correctly but late. Hence the shapes
below. Reasoning: docs/adr/0006-outbound-calls.md.

* Unavailability is a value, not an exception — the caller needs a policy for
  it, not a `try` several frames up.
* `REFUSED` and `UNAVAILABLE` never merge. An adapter that reports a timeout as
  a refusal turns a network incident into a fraud spike on every dashboard.
* No HTTP in the signatures, so the transport can change without the caller.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from evenkeel.domain.entities.ledger_entry import LedgerDirection
from evenkeel.domain.value_objects.ids import OwnerId, WalletId
from evenkeel.domain.value_objects.money import Money


class RiskOutcome(StrEnum):
    ALLOWED = "allowed"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RiskCheck:
    """What the provider is asked about.

    Carries domain types rather than primitives: an adapter that receives
    `Money` cannot lose the currency, and one that receives `amount: float`
    already has.

    The idempotency key is passed through deliberately. The provider is a
    separate system with its own retries, and giving it the same key the client
    gave us means our retry and its deduplication agree on what "the same
    request" is.
    """

    wallet_id: WalletId
    owner_id: OwnerId
    amount: Money
    direction: LedgerDirection
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """An answer, or an admission that there is none.

    `reason` is provider prose destined for a log line and a support
    conversation — never for a branch, and never rendered to the client
    verbatim, since it is text this service does not control.
    """

    outcome: RiskOutcome
    reason: str = ""
    reference: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome is RiskOutcome.ALLOWED

    @property
    def available(self) -> bool:
        return self.outcome is not RiskOutcome.UNAVAILABLE


class RiskAssessmentPort(ABC):
    """Ask an external provider whether a movement may proceed.

    Implementations must not raise for anything they can foresee: a timeout, a
    refused connection, a 500, a malformed body and a full bulkhead are all
    `UNAVAILABLE`. Raising leaks the transport into the caller
    and, worse, makes "the provider is having a bad day" indistinguishable from
    "this code has a bug".
    """

    @abstractmethod
    async def assess(self, check: RiskCheck) -> RiskDecision: ...


__all__ = [
    "RiskAssessmentPort",
    "RiskCheck",
    "RiskDecision",
    "RiskOutcome",
]
