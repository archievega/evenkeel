"""The one outbound call on the money path.

Every other port in this package hides a piece of infrastructure the process
owns: its database, its Redis, its clock. This one hides a *service someone else
operates* — which is a different kind of dependency and fails in ways the others
do not. It can be slow rather than down. It can answer correctly and late. It
can be up while the network to it is not. It can change its response schema on a
Tuesday without telling you.

Three consequences visible in the shapes below.

**Unavailability is a value, not an exception.** `RiskOutcome.UNAVAILABLE` sits
beside `ALLOWED` and `REFUSED` because "the check could not run" is an ordinary
operational state that the caller must have a policy for, not an accident to be
caught several frames up by whoever happens to have a `try`. The same reasoning
as `DistributedLock.acquired` and `BulkheadLease.acquired`.

**Refused and unavailable are never merged.** They mean opposite things: one is
the provider working, the other is the provider missing. An adapter that
returns "refuse" when it times out has invented a decision, and every dashboard
downstream will report a fraud spike during a network incident.

**The port says nothing about HTTP.** No status codes, no headers, no retry
count. Those live in the adapter, so the application can be tested against a
decision table and the transport can be replaced by gRPC, a queue, or a local
model without a single change above this line.
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
    refused connection, a 500, a malformed body, a full bulkhead and an open
    circuit are all `UNAVAILABLE`. Raising leaks the transport into the caller
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
