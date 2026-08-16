"""The wallet as MCP tools, over the same interactors the HTTP router uses.

Adding this changed nothing below `presentation` — no port, no command, no
branch in a use case — which is the only way to show the layering is real
rather than decorative. Reasoning: docs/adr/0007-mcp-as-a-second-transport.md.

Two rules this file exists to keep:

* The owner is bound at construction, from configuration. A tool taking an
  `owner_id` would be a cross-tenant IDOR with a natural-language interface.
* No status codes. MCP has no 404; the interactors never had one either.
"""

import functools
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from dishka import AsyncContainer
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from evenkeel.application.errors import ApplicationError, ApplicationErrorCode
from evenkeel.application.interactors.wallets import (
    DepositCommand,
    DepositToWalletInteractor,
    GetWalletInteractor,
    GetWalletQuery,
    ListLedgerEntriesInteractor,
    ListLedgerQuery,
    ListWalletsInteractor,
    ListWalletsQuery,
    OpenWalletCommand,
    OpenWalletInteractor,
    WithdrawCommand,
    WithdrawFromWalletInteractor,
)
from evenkeel.domain.errors import DomainError
from evenkeel.domain.value_objects.ids import OwnerId, WalletId
from evenkeel.domain.value_objects.money import CurrencyCode, Money

# A movement is not idempotent and not reversible; a read is neither. Clients
# use these hints to decide what to confirm with a human, so getting them wrong
# is not a documentation problem.
_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False
)

_IDEMPOTENCY_HELP = (
    "Optional. Reuse the same value when retrying a call you are unsure "
    "completed: the movement is then applied once and the original result is "
    "returned with replayed=true. A new value on a retry moves money again."
)


def create_mcp_server(container: AsyncContainer, owner: OwnerId) -> MCPServer:
    """Build the tool surface for one owner.

    `owner` is bound here, at construction, from configuration — see the module
    docstring for why it is not a parameter on the tools.
    """
    server = MCPServer(
        name="evenkeel",
        instructions=(
            "A wallet ledger. Amounts are decimal strings such as '10.00', "
            "never numbers, and every wallet already belongs to the "
            "authenticated owner — there is no parameter for whose wallet to "
            "touch. Movements are refused rather than adjusted when they would "
            "overdraw."
        ),
    )

    @server.tool(annotations=_WRITE, title="Open a wallet")
    @_translated
    async def open_wallet(currency: str) -> dict[str, Any]:
        """Open a new wallet in the given ISO 4217 currency, e.g. 'EUR'."""
        async with _use_case(container, OpenWalletInteractor) as interactor:
            result = await interactor(
                OpenWalletCommand(owner_id=owner, currency=_currency(currency))
            )
        return {
            "wallet_id": str(result.wallet_id.value),
            "currency": result.currency.value,
        }

    @server.tool(annotations=_WRITE, title="Deposit into a wallet")
    @_translated
    async def deposit(
        wallet_id: str,
        amount: str,
        currency: str,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Credit a wallet. `amount` is a decimal string such as '10.00'.

        idempotency_key: {help}
        """
        async with _use_case(container, DepositToWalletInteractor) as interactor:
            result = await interactor(
                DepositCommand(
                    wallet_id=_wallet_id(wallet_id),
                    owner_id=owner,
                    amount=_money(amount, currency),
                    description=description or "deposit",
                    idempotency_key=idempotency_key,
                )
            )
        return _movement(result)

    @server.tool(annotations=_WRITE, title="Withdraw from a wallet")
    @_translated
    async def withdraw(
        wallet_id: str,
        amount: str,
        currency: str,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Debit a wallet. Refused if it would overdraw; nothing is adjusted.

        idempotency_key: {help}
        """
        async with _use_case(container, WithdrawFromWalletInteractor) as interactor:
            result = await interactor(
                WithdrawCommand(
                    wallet_id=_wallet_id(wallet_id),
                    owner_id=owner,
                    amount=_money(amount, currency),
                    description=description or "withdrawal",
                    idempotency_key=idempotency_key,
                )
            )
        return _movement(result)

    @server.tool(annotations=_READ, title="Read a wallet")
    @_translated
    async def get_wallet(wallet_id: str) -> dict[str, Any]:
        """Current balance and currency of one wallet."""
        async with _use_case(container, GetWalletInteractor) as interactor:
            wallet = await interactor(
                GetWalletQuery(wallet_id=_wallet_id(wallet_id), owner_id=owner)
            )
        return {
            "wallet_id": str(wallet.id_.value),
            "balance": str(wallet.balance.amount),
            "currency": wallet.balance.currency.value,
            "status": wallet.status.value,
        }

    @server.tool(annotations=_READ, title="List wallets")
    @_translated
    async def list_wallets(limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        """Every wallet of the authenticated owner, newest first."""
        async with _use_case(container, ListWalletsInteractor) as interactor:
            page = await interactor(
                ListWalletsQuery(owner_id=owner, limit=limit, cursor=cursor)
            )
        return {
            "wallets": [
                {
                    "wallet_id": str(w.id_.value),
                    "balance": str(w.balance.amount),
                    "currency": w.balance.currency.value,
                }
                for w in page.items
            ],
            "next_cursor": page.next_cursor,
        }

    @server.tool(annotations=_READ, title="Read the ledger")
    @_translated
    async def list_entries(
        wallet_id: str, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        """Ledger history of one wallet, newest first."""
        async with _use_case(container, ListLedgerEntriesInteractor) as interactor:
            page = await interactor(
                ListLedgerQuery(
                    wallet_id=_wallet_id(wallet_id),
                    owner_id=owner,
                    limit=limit,
                    cursor=cursor,
                )
            )
        return {
            "entries": [
                {
                    "entry_id": str(e.id_.value),
                    "direction": e.direction.value,
                    "amount": str(e.amount.amount),
                    "balance_after": str(e.balance_after.amount),
                    "description": e.description,
                    "occurred_at": e.occurred_at.isoformat(),
                }
                for e in page.items
            ],
            "next_cursor": page.next_cursor,
        }

    for tool in (deposit, withdraw):
        # The parameter help is one string in one place; substituting it here
        # keeps the two tool descriptions from drifting apart.
        tool.__doc__ = (tool.__doc__ or "").format(help=_IDEMPOTENCY_HELP)

    return server


def _translated(
    tool: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Turn a use-case failure into something a model can act on.

    The same job the HTTP error handler does, for a transport with no status
    codes: a refusal arrives as its code and its human sentence, and nothing
    else. `functools.wraps` matters more than usual here — the tool schema the
    client sees is derived from the signature and docstring, so a wrapper that
    loses them silently publishes an empty schema.

    `details` are deliberately not forwarded. On the HTTP side they are a
    documented shape; here they would be `DomainError.context`, which is
    unfiltered and occasionally carries the value that was rejected. A new
    surface should not inherit a known gap.
    """

    @functools.wraps(tool)
    async def run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await tool(*args, **kwargs)
        except ApplicationError as failure:
            raise ToolError(f"{failure.code.value}: {failure.message}") from failure
        except DomainError as failure:
            raise ToolError(
                f"{failure.code.value}: "
                f"{failure.code.value.replace('_', ' ').capitalize()}"
            ) from failure

    return run


class _UseCase:
    """One request scope per tool call, closed even when the call raises.

    The same boundary an HTTP request gets: a session, a transaction, and the
    interactors resolved against them. Sharing one scope across tool calls would
    share one transaction across unrelated operations, which is how a failed
    call rolls back a successful one.
    """

    def __init__(self, container: AsyncContainer, use_case: type) -> None:
        self._container = container
        self._use_case = use_case
        self._scope: Any = None

    async def __aenter__(self) -> Any:
        self._scope = self._container()
        request_container = await self._scope.__aenter__()
        return await request_container.get(self._use_case)

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._scope.__aexit__(exc_type, exc, tb)


def _use_case(container: AsyncContainer, use_case: type) -> _UseCase:
    return _UseCase(container, use_case)


def _movement(result: Any) -> dict[str, Any]:
    return {
        "entry_id": str(result.entry_id.value),
        "wallet_id": str(result.wallet_id.value),
        "balance": str(result.balance.amount),
        "currency": result.balance.currency.value,
        "replayed": result.replayed,
    }


def _money(amount: str, currency: str) -> Money:
    """Parse what the model sent, refuse what it cannot mean.

    A model will send `10`, `10.0`, `"10.00"` and occasionally `ten`. The first
    three are fine; the fourth has to fail as a validation error rather than as
    a `TypeError` from three frames deeper, or the tool result is a stack trace
    and the model's next move is a guess.
    """
    try:
        parsed = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise _invalid(f"amount must be a decimal string, got {amount!r}") from exc
    if not parsed.is_finite():
        raise _invalid("amount must be a finite number")
    return Money(amount=parsed, currency=_currency(currency))


def _currency(code: str) -> CurrencyCode:
    try:
        return CurrencyCode(str(code).upper())
    except DomainError as exc:
        raise _invalid(f"currency must be an ISO 4217 code, got {code!r}") from exc


def _wallet_id(value: str) -> WalletId:
    from uuid import UUID

    try:
        return WalletId(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise _invalid(f"wallet_id must be a UUID, got {value!r}") from exc


def _invalid(message: str) -> ApplicationError:
    error = ApplicationError(ApplicationErrorCode.VALIDATION_FAILED)
    error.message = message
    return error


__all__ = ["create_mcp_server"]
