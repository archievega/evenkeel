import asyncio
from functools import partial
from uuid import uuid4

from httpx import AsyncClient

from evenkeel.domain.value_objects.ids import OwnerId


def auth(owner_id: OwnerId) -> dict[str, str]:
    return {"Authorization": f"Bearer {owner_id.value}"}


async def open_wallet(client: AsyncClient, owner_id: OwnerId) -> str:
    response = await client.post(
        "/v1/wallets", json={"currency": "EUR"}, headers=auth(owner_id)
    )
    assert response.status_code == 201
    return str(response.json()["id"])


async def test_a_request_without_credentials_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/v1/wallets", json={"currency": "EUR"})

    assert response.status_code == 401


async def test_deposit_then_withdraw_leaves_the_expected_balance(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    wallet_id = await open_wallet(client, owner_id)

    await client.post(
        f"/v1/wallets/{wallet_id}/deposits",
        json={"amount": "100.00", "currency": "EUR"},
        headers=auth(owner_id),
    )
    response = await client.post(
        f"/v1/wallets/{wallet_id}/withdrawals",
        json={"amount": "30.00", "currency": "EUR"},
        headers=auth(owner_id),
    )

    assert response.status_code == 200
    assert response.json()["balance"] == "70.00"


async def test_overdraft_is_refused_with_a_problem_document(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    wallet_id = await open_wallet(client, owner_id)

    response = await client.post(
        f"/v1/wallets/{wallet_id}/withdrawals",
        json={"amount": "1.00", "currency": "EUR"},
        headers=auth(owner_id),
    )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "WALLET_INSUFFICIENT_FUNDS"
    assert body["status"] == 409
    assert "correlation_id" in body


async def test_a_retried_deposit_is_applied_once(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    wallet_id = await open_wallet(client, owner_id)
    payload = {"amount": "25.00", "currency": "EUR"}
    headers = auth(owner_id) | {"Idempotency-Key": "client-retry-1"}

    first = await client.post(
        f"/v1/wallets/{wallet_id}/deposits", json=payload, headers=headers
    )
    second = await client.post(
        f"/v1/wallets/{wallet_id}/deposits", json=payload, headers=headers
    )

    assert first.json()["balance"] == "25.00"
    assert second.json()["balance"] == "25.00"
    assert second.json()["replayed"] is True
    assert first.json()["entry_id"] == second.json()["entry_id"]


async def test_one_owners_idempotency_key_does_not_collide_with_anothers(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    """The keyspace is per owner, because the key is a string clients invent.

    `k-42`, `1`, `retry` and today's date all get chosen, by more than one
    caller. Unscoped, the second owner to use a popular key is told it "was
    already used with a different payload" — a 409 for an operation they never
    made, and an oracle telling them which keys somebody else is using.

    Found by recording the README demo twice with the same key, not by a test,
    which is why this one exists.
    """
    other_owner = OwnerId(uuid4())
    mine = await open_wallet(client, owner_id)
    theirs = await open_wallet(client, other_owner)
    payload = {"amount": "5.00", "currency": "EUR"}
    key = {"Idempotency-Key": "k-42"}

    first = await client.post(
        f"/v1/wallets/{mine}/deposits", json=payload, headers=auth(owner_id) | key
    )
    second = await client.post(
        f"/v1/wallets/{theirs}/deposits", json=payload, headers=auth(other_owner) | key
    )

    assert first.status_code == 200
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is False
    assert first.json()["entry_id"] != second.json()["entry_id"]


async def test_reusing_a_key_with_a_different_payload_is_still_refused(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    """Scoping the keyspace must not soften the check inside it."""
    wallet_id = await open_wallet(client, owner_id)
    key = {"Idempotency-Key": "k-42"}

    await client.post(
        f"/v1/wallets/{wallet_id}/deposits",
        json={"amount": "5.00", "currency": "EUR"},
        headers=auth(owner_id) | key,
    )
    changed = await client.post(
        f"/v1/wallets/{wallet_id}/deposits",
        json={"amount": "6.00", "currency": "EUR"},
        headers=auth(owner_id) | key,
    )

    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_another_owner_sees_the_wallet_as_absent(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    """404, not 403.

    Answering "it exists, but is not yours" turns the endpoint into an
    existence oracle: an attacker learns which ids are real from the status
    code alone, without ever seeing the data.
    """
    wallet_id = await open_wallet(client, owner_id)

    response = await client.get(
        f"/v1/wallets/{wallet_id}", headers=auth(OwnerId(uuid4()))
    )

    assert response.status_code == 404


async def test_another_owner_cannot_read_the_ledger(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    """The read path is a separate opportunity to leak, so it gets its own test."""
    wallet_id = await open_wallet(client, owner_id)

    response = await client.get(
        f"/v1/wallets/{wallet_id}/entries", headers=auth(OwnerId(uuid4()))
    )

    assert response.status_code == 404


async def test_a_negative_amount_is_rejected_at_the_edge(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    wallet_id = await open_wallet(client, owner_id)

    response = await client.post(
        f"/v1/wallets/{wallet_id}/deposits",
        json={"amount": "-5.00", "currency": "EUR"},
        headers=auth(owner_id),
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_FAILED"
    # The rejected value never comes back. Pydantic puts it in `input`, and a
    # validation report is the easiest place to accidentally echo a card number
    # or a password typed into the wrong field.
    error = body["details"]["errors"][0]
    assert set(error) >= {"loc", "msg", "type"}
    assert "-5.00" not in str(body)


async def test_an_unknown_wallet_is_a_404(client: AsyncClient, owner_id: OwnerId) -> None:
    response = await client.get(f"/v1/wallets/{uuid4()}", headers=auth(owner_id))

    assert response.status_code == 404
    assert response.json()["code"] == "WALLET_NOT_FOUND"


async def test_every_response_carries_a_correlation_id(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    response = await client.get("/v1/wallets", headers=auth(owner_id))

    assert response.headers["X-Correlation-ID"]


async def test_an_inbound_correlation_id_is_preserved(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    response = await client.get(
        "/v1/wallets",
        headers=auth(owner_id) | {"X-Correlation-ID": "trace-me"},
    )

    assert response.headers["X-Correlation-ID"] == "trace-me"


async def test_the_ledger_records_both_movements(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    wallet_id = await open_wallet(client, owner_id)
    await client.post(
        f"/v1/wallets/{wallet_id}/deposits",
        json={"amount": "50.00", "currency": "EUR"},
        headers=auth(owner_id),
    )
    await client.post(
        f"/v1/wallets/{wallet_id}/withdrawals",
        json={"amount": "20.00", "currency": "EUR"},
        headers=auth(owner_id),
    )

    response = await client.get(
        f"/v1/wallets/{wallet_id}/entries", headers=auth(owner_id)
    )

    entries = response.json()["items"]
    assert {e["direction"] for e in entries} == {"credit", "debit"}
    assert {e["amount"] for e in entries} == {"50.00", "20.00"}


async def test_a_replay_reports_the_balance_it_was_written_with(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    """ADR 4 promises "the original result". It used to return the original
    entry beside the *current* balance, so a wallet that moved on in between
    produced one response disagreeing with itself."""
    wallet_id = await open_wallet(client, owner_id)
    key = {"Idempotency-Key": "receipt"}
    payload = {"amount": "10.00", "currency": "EUR"}

    first = await client.post(
        f"/v1/wallets/{wallet_id}/deposits", json=payload, headers=auth(owner_id) | key
    )
    await client.post(
        f"/v1/wallets/{wallet_id}/deposits",
        json={"amount": "5.00", "currency": "EUR"},
        headers=auth(owner_id),
    )
    replay = await client.post(
        f"/v1/wallets/{wallet_id}/deposits", json=payload, headers=auth(owner_id) | key
    )

    assert replay.json()["replayed"] is True
    assert replay.json()["balance"] == first.json()["balance"] == "10.00"
    assert replay.json()["entry_id"] == first.json()["entry_id"]


async def test_two_concurrent_requests_with_one_key_move_money_once(
    client: AsyncClient, owner_id: OwnerId
) -> None:
    """Which of the two answers arrives is timing; that the money moved once is
    not.

    Asserted as the invariant rather than as a pair of status codes: through the
    in-memory store the first request often finishes before the second claims
    the key, so the second replays with 200 instead of being refused with 409.
    Both are correct. `tests/unit/test_movement_concurrency.py` pins the refusal
    itself, with a store that suspends on every call the way a network does.
    """
    wallet_id = await open_wallet(client, owner_id)
    key = {"Idempotency-Key": "concurrent"}
    payload = {"amount": "1.00", "currency": "EUR"}
    post = partial(
        client.post,
        f"/v1/wallets/{wallet_id}/deposits",
        json=payload,
        headers=auth(owner_id) | key,
    )

    both = await asyncio.gather(post(), post())

    assert {r.status_code for r in both} <= {200, 409}
    applied = [r for r in both if r.status_code == 200 and not r.json()["replayed"]]
    assert len(applied) == 1, "the movement was applied more than once"
    balance = await client.get(f"/v1/wallets/{wallet_id}", headers=auth(owner_id))
    assert balance.json()["balance"] == "1.00"
