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
from evenkeel.application.ports import IdempotencyRecord, IdempotencyStore
from evenkeel.domain.entities.ledger_entry import LedgerDirection
from evenkeel.domain.errors import DomainError, DomainErrorCode
from tests.unit.test_wallet_movement import Harness


class YieldingIdempotencyStore(IdempotencyStore):
    """In-memory, but every operation suspends like a real round trip."""

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}

    async def get(self, key: str) -> IdempotencyRecord | None:
        await asyncio.sleep(0)
        return self._records.get(key)

    async def put(self, record: IdempotencyRecord, *, ttl_seconds: int) -> None:
        await asyncio.sleep(0)
        self._records[record.key] = record


def harness_with_yielding_store() -> Harness:
    harness = Harness(balance="100.00")
    harness.service._idempotency = YieldingIdempotencyStore()
    return harness


async def test_two_in_flight_requests_with_one_key_apply_once() -> None:
    """The claim `docs/SECURITY_CONTROLS.md` makes about CWE-837.

    Without serialising the replay check, both callers miss the store, both
    queue on the lock, and both apply — 20.00 leaves a wallet that authorised
    10.00.
    """
    harness = harness_with_yielding_store()
    request = harness.request(
        "10.00", direction=LedgerDirection.DEBIT, idempotency_key="double-tap"
    )

    results = await asyncio.gather(
        harness.service.apply(request), harness.service.apply(request)
    )

    assert harness.wallet.balance.amount == Decimal("90.00")
    assert len(harness.ledger.entries) == 1
    assert harness.transaction_manager.commits == 1
    assert sum(1 for r in results if r.replayed) == 1
    assert results[0].entry.id_ == results[1].entry.id_


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
