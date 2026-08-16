"""Concurrency on the money path, as opposed to sequential repetition.

Every idempotency test before this one called `apply()` twice in a row. That
proves a client retry after a response is safe, which is the easy half. The half
that costs money is two requests carrying the same key that are in flight at the
same time — a double-tap, a proxy retry, a client with no backoff.

The store here yields control on every await. That is not a contrivance to force
a failure: it is what a network call to Redis does. A store that never suspends
hides the interleaving and would let this suite certify a bug as fixed.
"""

import asyncio
from decimal import Decimal

import pytest

from evenkeel.application.errors import ApplicationErrorCode, ConflictError
from evenkeel.application.ports import IdempotencyReservation
from evenkeel.application.services.wallet_movement import MovementOutcome
from evenkeel.domain.entities.ledger_entry import LedgerDirection
from evenkeel.domain.errors import DomainError, DomainErrorCode
from evenkeel.infrastructure.adapters.memory.idempotency import InMemoryIdempotencyStore
from tests.unit.test_wallet_movement import Harness


class YieldingIdempotencyStore(InMemoryIdempotencyStore):
    """The real in-memory store, suspending like a round trip on every call.

    The suspension is the test. Without it the claim looks atomic because
    nothing else ever gets to run between its read and its write, which is
    exactly the illusion a single-process test creates and a network destroys.
    """

    async def reserve(self, key: str, **kwargs: object) -> IdempotencyReservation:
        await asyncio.sleep(0)
        return await super().reserve(key, **kwargs)  # type: ignore[arg-type]

    async def confirm(self, key: str, **kwargs: object) -> None:
        await asyncio.sleep(0)
        await super().confirm(key, **kwargs)  # type: ignore[arg-type]

    async def release(self, key: str) -> None:
        await asyncio.sleep(0)
        await super().release(key)


def harness_with_yielding_store() -> Harness:
    harness = Harness(balance="100.00")
    harness.service._idempotency = YieldingIdempotencyStore()
    return harness


@pytest.mark.cwe(837)
async def test_two_in_flight_requests_with_one_key_apply_once() -> None:
    """The claim `docs/SECURITY_CONTROLS.md` makes about CWE-837.

    Without an atomic claim both callers miss the store, both queue on the lock,
    and both apply — 20.00 leaves a wallet that authorised 10.00.

    The loser is refused rather than answered. There is no result to replay yet:
    the winner has not finished, and inventing one would be a receipt for
    something that may still fail. Stripe answers a concurrent duplicate the
    same way, and the client's move is the same as for any 409 — ask again.
    """
    harness = harness_with_yielding_store()
    request = harness.request(
        "10.00", direction=LedgerDirection.DEBIT, idempotency_key="double-tap"
    )

    results = await asyncio.gather(
        harness.service.apply(request),
        harness.service.apply(request),
        return_exceptions=True,
    )

    applied = [r for r in results if isinstance(r, MovementOutcome)]
    refused = [r for r in results if isinstance(r, ConflictError)]
    assert len(applied) == 1
    assert len(refused) == 1
    assert refused[0].code is ApplicationErrorCode.MOVEMENT_IN_PROGRESS
    assert harness.wallet.balance.amount == Decimal("90.00")
    assert len(harness.ledger.entries) == 1
    assert harness.transaction_manager.commits == 1


async def test_the_retry_that_arrives_after_the_original_finishes_replays() -> None:
    """The common case, and the one that must not be a 409: a client that gave
    up waiting and asked again."""
    harness = harness_with_yielding_store()
    request = harness.request(
        "10.00", direction=LedgerDirection.DEBIT, idempotency_key="retry-later"
    )

    first = await harness.service.apply(request)
    second = await harness.service.apply(request)

    assert first.replayed is False
    assert second.replayed is True
    assert second.entry.id_ == first.entry.id_
    assert harness.wallet.balance.amount == Decimal("90.00")
    assert harness.transaction_manager.commits == 1


async def test_a_refused_movement_gives_the_key_back() -> None:
    """A key burned by a refusal would make the client wait out a 24 hour TTL
    before it could retry something that never happened."""
    harness = Harness(balance="5.00")
    harness.service._idempotency = YieldingIdempotencyStore()
    too_much = harness.request(
        "10.00", direction=LedgerDirection.DEBIT, idempotency_key="same-key"
    )

    with pytest.raises(DomainError):
        await harness.service.apply(too_much)
    affordable = harness.request(
        "5.00", direction=LedgerDirection.DEBIT, idempotency_key="same-key"
    )
    result = await harness.service.apply(affordable)

    assert result.replayed is False
    assert harness.wallet.balance.amount == Decimal("0.00")


async def test_concurrent_requests_without_a_key_both_apply() -> None:
    """The control case, so the fix above cannot be an accidental global lock.

    Two deliberate movements with no idempotency key are two movements.
    """
    harness = harness_with_yielding_store()

    await asyncio.gather(
        harness.service.apply(harness.request("10.00")),
        harness.service.apply(harness.request("10.00")),
    )

    assert harness.wallet.balance.amount == Decimal("80.00")
    assert len(harness.ledger.entries) == 2
    assert harness.transaction_manager.commits == 2


async def test_a_domain_failure_rolls_the_transaction_back() -> None:
    """Every other failure branch rolls back explicitly; this one did not.

    An interactor that raises without rolling back hands the next user of that
    session a transaction with someone else's uncommitted writes in it.
    """
    harness = Harness(balance="5.00")

    with pytest.raises(DomainError) as error:
        await harness.service.apply(
            harness.request("50.00", direction=LedgerDirection.DEBIT)
        )

    assert error.value.code is DomainErrorCode.WALLET_INSUFFICIENT_FUNDS
    assert harness.transaction_manager.rollbacks == 1
    assert harness.transaction_manager.commits == 0


async def test_the_fingerprint_covers_the_description() -> None:
    """Same key, same amount, different description is a different request.

    Replaying it returns a receipt for an operation the caller did not make.
    """
    harness = harness_with_yielding_store()

    await harness.service.apply(
        harness.request("10.00", description="rent", idempotency_key="k")
    )

    with pytest.raises(ConflictError) as error:
        await harness.service.apply(
            harness.request("10.00", description="groceries", idempotency_key="k")
        )

    assert error.value.code is ApplicationErrorCode.IDEMPOTENCY_KEY_REUSED
