"""What a client sees when the risk provider refuses, or cannot be reached.

Through the real HTTP stack, because the interesting assertions are about the
status code and the problem document — the parts a unit test on the interactor
would have to guess at. The provider itself is the scripted fake; the transport
that talks to a real one is covered in `tests/unit/test_outbound_transport.py`.
"""

from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient

from evenkeel.application.ports.risk import RiskDecision, RiskOutcome
from evenkeel.domain.value_objects.ids import OwnerId
from tests.fakes.system import ScriptedRiskAssessment
from tests.http.test_wallets_api import auth, open_wallet


def body(amount: str = "10.00") -> dict[str, Any]:
    return {"amount": amount, "currency": "EUR"}


@pytest.fixture
async def funded_wallet(
    client: AsyncClient, owner_id: OwnerId, risk: ScriptedRiskAssessment
) -> str:
    """A wallet with 100.00 in it, funded while the provider still allows.

    `risk.calls` is cleared afterwards so a test can assert on what the provider
    was asked about its own movement rather than about this setup.
    """
    wallet_id = await open_wallet(client, owner_id)
    await client.post(
        f"/v1/wallets/{wallet_id}/deposits", json=body("100.00"), headers=auth(owner_id)
    )
    risk.calls.clear()
    return wallet_id


async def test_a_refusal_is_a_403_problem_document(
    client: AsyncClient,
    owner_id: OwnerId,
    funded_wallet: str,
    risk: ScriptedRiskAssessment,
) -> None:
    risk.decision = RiskDecision(
        outcome=RiskOutcome.REFUSED, reason="velocity rule 7", reference="ref-42"
    )

    response = await client.post(
        f"/v1/wallets/{funded_wallet}/withdrawals",
        json=body(),
        headers=auth(owner_id),
    )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = response.json()
    assert problem["code"] == "MOVEMENT_REFUSED"
    assert problem["details"] == {"reference": "ref-42"}


async def test_a_refusal_does_not_say_which_rule_fired(
    client: AsyncClient,
    owner_id: OwnerId,
    funded_wallet: str,
    risk: ScriptedRiskAssessment,
) -> None:
    """The provider's prose stays server-side. Handing it to the caller turns
    every refusal into a hint about how to shape the next attempt."""
    risk.decision = RiskDecision(
        outcome=RiskOutcome.REFUSED, reason="velocity rule 7 exceeded", reference="r"
    )

    response = await client.post(
        f"/v1/wallets/{funded_wallet}/withdrawals",
        json=body(),
        headers=auth(owner_id),
    )

    assert "velocity" not in response.text
    assert "rule" not in response.text


async def test_a_refused_movement_moves_nothing(
    client: AsyncClient,
    owner_id: OwnerId,
    funded_wallet: str,
    risk: ScriptedRiskAssessment,
) -> None:
    risk.decision = RiskDecision(outcome=RiskOutcome.REFUSED)

    await client.post(
        f"/v1/wallets/{funded_wallet}/withdrawals",
        json=body(),
        headers=auth(owner_id),
    )
    after = await client.get(f"/v1/wallets/{funded_wallet}", headers=auth(owner_id))

    assert after.json()["balance"] == "100.00"


async def test_an_unreachable_provider_stops_the_movement_by_default(
    client: AsyncClient,
    owner_id: OwnerId,
    funded_wallet: str,
    risk: ScriptedRiskAssessment,
) -> None:
    """Fail closed. An unassessed movement is permanent; a 503 is not."""
    risk.decision = RiskDecision(outcome=RiskOutcome.UNAVAILABLE, reason="timeout")

    response = await client.post(
        f"/v1/wallets/{funded_wallet}/withdrawals",
        json=body(),
        headers=auth(owner_id),
    )

    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.json()["details"] == {"dependency": "risk"}


async def test_the_provider_is_asked_about_the_actual_movement(
    client: AsyncClient,
    owner_id: OwnerId,
    funded_wallet: str,
    risk: ScriptedRiskAssessment,
) -> None:
    """A check that is passed the wrong amount or the wrong direction is worse
    than no check: it produces an audit trail that says the wrong thing."""
    await client.post(
        f"/v1/wallets/{funded_wallet}/withdrawals",
        json=body("25.50"),
        headers=auth(owner_id) | {"Idempotency-Key": "key-1"},
    )

    assert len(risk.calls) == 1
    check = risk.calls[0]
    assert check.amount.amount == Decimal("25.50")
    assert check.direction.value == "debit"
    assert str(check.wallet_id.value) == funded_wallet
    assert check.owner_id == owner_id
    # Passed through so the provider can deduplicate on the same key we do.
    assert check.idempotency_key == "key-1"


async def test_an_allowed_movement_is_unaffected(
    client: AsyncClient, owner_id: OwnerId, funded_wallet: str
) -> None:
    response = await client.post(
        f"/v1/wallets/{funded_wallet}/deposits",
        json=body("10.00"),
        headers=auth(owner_id),
    )

    assert response.status_code == 200
    assert response.json()["balance"] == "110.00"
