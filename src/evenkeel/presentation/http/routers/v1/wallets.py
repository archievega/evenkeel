from decimal import Decimal
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Depends, Query, status

from evenkeel.application.interactors.wallets import (
    DepositCommand,
    DepositToWalletInteractor,
    GetWalletInteractor,
    GetWalletQuery,
    ListLedgerEntriesInteractor,
    ListLedgerQuery,
    ListWalletsInteractor,
    ListWalletsQuery,
    MovementResult,
    OpenWalletCommand,
    OpenWalletInteractor,
    WithdrawCommand,
    WithdrawFromWalletInteractor,
)
from evenkeel.domain.value_objects.ids import WalletId
from evenkeel.domain.value_objects.money import CurrencyCode, Money
from evenkeel.presentation.http.dependencies import (
    CurrentPrincipal,
    IdempotencyKey,
    current_principal,
)
from evenkeel.presentation.http.responses import (
    collection_responses,
    movement_responses,
    read_responses,
)
from evenkeel.presentation.http.schemas.v1.wallets import (
    LedgerEntryResponse,
    LedgerPageResponse,
    MoneyRequest,
    MovementResponse,
    OpenWalletRequest,
    WalletPageResponse,
    WalletResponse,
)

# Authentication is declared once, on the router. Per-endpoint `Depends` makes
# every new route opt in to being protected, and the route that forgets is
# indistinguishable from one that is intentionally public -- which is how an
# unauthenticated password-reset endpoint ships. A public route here has to say
# so explicitly.
router = APIRouter(route_class=DishkaRoute, dependencies=[Depends(current_principal)])


@router.post(
    "",
    response_model=WalletResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open a wallet",
    responses=collection_responses(),
)
async def open_wallet(
    payload: OpenWalletRequest,
    principal: CurrentPrincipal,
    interactor: FromDishka[OpenWalletInteractor],
) -> WalletResponse:
    """Handlers stay this thin.

    Parse, call one use case, shape the response. No business rules, no
    repository access, no transaction handling -- that is what keeps the same
    use case reusable from a worker, a CLI or an MCP tool.
    """
    result = await interactor(
        OpenWalletCommand(
            owner_id=principal.owner_id,
            currency=CurrencyCode(payload.currency.upper()),
        )
    )
    return WalletResponse(
        id=result.wallet_id.value,
        owner_id=principal.owner_id.value,
        balance=Decimal("0.00"),
        currency=result.currency.value,
        status="open",
        version=0,
    )


@router.get(
    "",
    response_model=WalletPageResponse,
    summary="List the caller's wallets",
    responses=collection_responses(),
)
async def list_wallets(
    principal: CurrentPrincipal,
    interactor: FromDishka[ListWalletsInteractor],
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> WalletPageResponse:
    """Cursor-paginated, newest first.

    Pass `next_cursor` back as `cursor` to walk further. Offsets are not
    supported on purpose: they make deep pages linearly slower and let
    concurrent inserts shift rows between pages.
    """
    page = await interactor(
        ListWalletsQuery(owner_id=principal.owner_id, limit=limit, cursor=cursor)
    )
    return WalletPageResponse(
        items=[WalletResponse.of(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/{wallet_id}",
    response_model=WalletResponse,
    summary="Read one wallet",
    responses=read_responses(),
)
async def get_wallet(
    wallet_id: UUID,
    principal: CurrentPrincipal,
    interactor: FromDishka[GetWalletInteractor],
) -> WalletResponse:
    """A wallet owned by someone else is reported as absent, not forbidden."""
    wallet = await interactor(
        GetWalletQuery(wallet_id=WalletId(wallet_id), owner_id=principal.owner_id)
    )
    return WalletResponse.of(wallet)


@router.post(
    "/{wallet_id}/deposits",
    response_model=MovementResponse,
    summary="Credit a wallet",
    responses=movement_responses(),
)
async def deposit(
    wallet_id: UUID,
    payload: MoneyRequest,
    principal: CurrentPrincipal,
    interactor: FromDishka[DepositToWalletInteractor],
    idempotency_key: IdempotencyKey = None,
) -> MovementResponse:
    """Send `Idempotency-Key` and a retry cannot apply twice.

    The response then carries `replayed: true` and the original `entry_id`.
    Reusing a key with a different payload is refused with 409 rather than
    silently confirmed.
    """
    result = await interactor(
        DepositCommand(
            wallet_id=WalletId(wallet_id),
            owner_id=principal.owner_id,
            amount=Money(
                amount=payload.amount,
                currency=CurrencyCode(payload.currency.upper()),
            ),
            description=payload.description,
            idempotency_key=idempotency_key,
        )
    )
    return _movement_response(result)


@router.post(
    "/{wallet_id}/withdrawals",
    response_model=MovementResponse,
    summary="Debit a wallet",
    responses=movement_responses(),
)
async def withdraw(
    wallet_id: UUID,
    payload: MoneyRequest,
    principal: CurrentPrincipal,
    interactor: FromDishka[WithdrawFromWalletInteractor],
    idempotency_key: IdempotencyKey = None,
) -> MovementResponse:
    """Refused with 409 if the balance is short; nothing is written.

    Accepts `Idempotency-Key` on the same terms as a deposit.
    """
    result = await interactor(
        WithdrawCommand(
            wallet_id=WalletId(wallet_id),
            owner_id=principal.owner_id,
            amount=Money(
                amount=payload.amount,
                currency=CurrencyCode(payload.currency.upper()),
            ),
            description=payload.description,
            idempotency_key=idempotency_key,
        )
    )
    return _movement_response(result)


@router.get(
    "/{wallet_id}/entries",
    response_model=LedgerPageResponse,
    summary="List ledger entries for a wallet",
    responses=read_responses(),
)
async def list_entries(
    wallet_id: UUID,
    principal: CurrentPrincipal,
    interactor: FromDishka[ListLedgerEntriesInteractor],
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> LedgerPageResponse:
    """Append-only history for one wallet, newest first.

    The balance is a cache of these entries, so replaying them reconstructs it.
    """
    page = await interactor(
        ListLedgerQuery(
            wallet_id=WalletId(wallet_id),
            owner_id=principal.owner_id,
            limit=limit,
            cursor=cursor,
        )
    )
    return LedgerPageResponse(
        items=[LedgerEntryResponse.of(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


def _movement_response(result: MovementResult) -> MovementResponse:
    return MovementResponse(
        entry_id=result.entry_id.value,
        wallet_id=result.wallet_id.value,
        balance=result.balance.amount,
        currency=result.balance.currency.value,
        replayed=result.replayed,
    )
