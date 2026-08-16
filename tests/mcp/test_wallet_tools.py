"""The second transport, over the same interactors.

The point of these tests is not that the tools work. It is that the guarantees
were inherited rather than re-implemented: the same idempotency, the same owner
scoping, the same refusal to overdraw, reached through a different door. If any
of them had to be written again for MCP, the layering would be decoration and
this file would be where that shows up.
"""

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("mcp", reason="requires the `mcp` extra")

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from evenkeel.domain.value_objects.ids import OwnerId
from evenkeel.presentation.mcp.server import create_mcp_server
from tests.fakes.container import build_container
from tests.fakes.repositories import (
    FakeLedgerRepository,
    FakeWalletRepository,
)
from tests.fakes.system import ScriptedRiskAssessment

MOVEMENT_TOOLS = ("open_wallet", "deposit", "withdraw")
READ_TOOLS = ("get_wallet", "list_wallets", "list_entries")


@pytest.fixture
def owner() -> OwnerId:
    return OwnerId(uuid4())


@pytest.fixture
def storage() -> tuple[FakeWalletRepository, FakeLedgerRepository]:
    return FakeWalletRepository(), FakeLedgerRepository()


@pytest.fixture
def server(
    owner: OwnerId, storage: tuple[FakeWalletRepository, FakeLedgerRepository]
) -> Iterator[MCPServer]:
    wallets, ledger = storage
    yield create_mcp_server(
        build_container(wallets, ledger, ScriptedRiskAssessment()), owner
    )


async def call(server: MCPServer, tool: str, **arguments: Any) -> dict[str, Any]:
    return _structured(await server.call_tool(tool, arguments))


async def call_expecting_failure(server: MCPServer, tool: str, **arguments: Any) -> str:
    """A refused tool call raises; the client turns that into an error result.

    Asserting on the message rather than on a type: what reaches the model is
    the string, and that is the part that has to carry a code and not a
    traceback.
    """
    with pytest.raises(ToolError) as failure:
        await server.call_tool(tool, arguments)
    return str(failure.value)


def _text(result: Any) -> str:
    return " ".join(
        getattr(block, "text", "") for block in getattr(result, "content", [])
    )


def _structured(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    return dict(json.loads(_text(result)))


async def open_wallet(server: MCPServer) -> str:
    return str((await call(server, "open_wallet", currency="EUR"))["wallet_id"])


async def test_the_tool_surface_is_the_use_cases(server: MCPServer) -> None:
    names = {tool.name for tool in await server.list_tools()}

    assert names == set(MOVEMENT_TOOLS) | set(READ_TOOLS)


async def test_no_tool_lets_the_caller_choose_an_owner(server: MCPServer) -> None:
    """The security property of this transport, asserted rather than trusted.

    An `owner_id` parameter here would be a cross-tenant IDOR with a
    natural-language interface: the model reads untrusted text for a living, and
    anything that persuades it to pass a different id moves somebody else's
    money. The owner is bound at construction, from configuration.
    """
    for tool in await server.list_tools():
        parameters = set(tool.input_schema.get("properties", {}))

        assert not {p for p in parameters if "owner" in p.lower()}, tool.name


async def test_movements_are_marked_destructive_and_reads_are_not(
    server: MCPServer,
) -> None:
    """Clients decide what to confirm with a human from these hints, so a
    mislabelled money movement is a missing confirmation dialog."""
    annotations = {tool.name: tool.annotations for tool in await server.list_tools()}

    for name in MOVEMENT_TOOLS:
        assert annotations[name] is not None
        assert annotations[name].destructive_hint is True, name
        assert annotations[name].read_only_hint is False, name
    for name in READ_TOOLS:
        assert annotations[name].read_only_hint is True, name


async def test_a_deposit_moves_money_and_a_read_sees_it(server: MCPServer) -> None:
    wallet_id = await open_wallet(server)

    await call(server, "deposit", wallet_id=wallet_id, amount="100.00", currency="EUR")
    wallet = await call(server, "get_wallet", wallet_id=wallet_id)

    assert wallet["balance"] == "100.00"
    assert wallet["currency"] == "EUR"


async def test_an_overdraft_is_refused_with_a_code_not_a_traceback(
    server: MCPServer,
) -> None:
    """MCP has no 409. The interactor never had one either — it raises a code,
    and each transport decides how to say it."""
    wallet_id = await open_wallet(server)
    await call(server, "deposit", wallet_id=wallet_id, amount="10.00", currency="EUR")

    message = await call_expecting_failure(
        server, "withdraw", wallet_id=wallet_id, amount="1000.00", currency="EUR"
    )

    assert "insufficient" in message.lower()
    assert "Traceback" not in message
    balance = (await call(server, "get_wallet", wallet_id=wallet_id))["balance"]
    assert balance == "10.00", "a refused withdrawal must move nothing"


async def test_the_idempotency_key_is_inherited_from_the_use_case(
    server: MCPServer,
) -> None:
    """Nothing in the MCP layer implements this. A model that retries a tool
    call it is unsure about gets the original result, because the interactor
    behind the tool is the one the HTTP router calls."""
    wallet_id = await open_wallet(server)

    first = await call(
        server,
        "deposit",
        wallet_id=wallet_id,
        amount="10.00",
        currency="EUR",
        idempotency_key="retry-1",
    )
    second = await call(
        server,
        "deposit",
        wallet_id=wallet_id,
        amount="10.00",
        currency="EUR",
        idempotency_key="retry-1",
    )

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["entry_id"] == first["entry_id"]
    assert second["balance"] == "10.00"


async def test_another_owners_wallet_is_invisible(
    storage: tuple[FakeWalletRepository, FakeLedgerRepository], server: MCPServer
) -> None:
    """Two servers, two owners, one storage — the owner scoping is in the SQL
    the repository writes, not in either transport."""
    wallets, ledger = storage
    stranger = create_mcp_server(
        build_container(wallets, ledger, ScriptedRiskAssessment()), OwnerId(uuid4())
    )
    wallet_id = await open_wallet(server)

    message = await call_expecting_failure(stranger, "get_wallet", wallet_id=wallet_id)

    assert "not found" in message.lower()


@pytest.mark.parametrize("amount", ["ten", "", "1e400", "NaN"])
async def test_an_amount_a_model_invented_fails_as_validation(
    server: MCPServer, amount: str
) -> None:
    """Models send `10`, `10.00` and occasionally `ten`. The last one has to be
    a validation error rather than a `TypeError` from three frames deeper, or
    the tool result is a stack trace and the model's next move is a guess."""
    wallet_id = await open_wallet(server)

    message = await call_expecting_failure(
        server, "deposit", wallet_id=wallet_id, amount=amount, currency="EUR"
    )

    assert "Traceback" not in message


async def test_a_number_shaped_amount_is_still_exact(server: MCPServer) -> None:
    """The wire carries strings, and what lands in the ledger is a Decimal."""
    wallet_id = await open_wallet(server)

    await call(server, "deposit", wallet_id=wallet_id, amount="0.10", currency="EUR")
    await call(server, "deposit", wallet_id=wallet_id, amount="0.20", currency="EUR")

    balance = (await call(server, "get_wallet", wallet_id=wallet_id))["balance"]
    assert Decimal(balance) == Decimal("0.30")


async def test_the_ledger_reads_back_both_movements(server: MCPServer) -> None:
    wallet_id = await open_wallet(server)
    await call(server, "deposit", wallet_id=wallet_id, amount="50.00", currency="EUR")
    await call(server, "withdraw", wallet_id=wallet_id, amount="20.00", currency="EUR")

    entries = (await call(server, "list_entries", wallet_id=wallet_id))["entries"]

    assert [e["direction"] for e in entries] == ["debit", "credit"]
    assert entries[0]["balance_after"] == "30.00"
