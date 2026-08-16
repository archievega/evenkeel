from uuid import uuid4

from httpx import AsyncClient

from appcore.domain.value_objects.ids import OwnerId


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
    assert response.json()["code"] == "VALIDATION_FAILED"


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
