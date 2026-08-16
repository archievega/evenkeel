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


@router.post("", response_model=WalletResponse, status_code=status.HTTP_201_CREATED)
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


@router.get("", response_model=WalletPageResponse)
async def list_wallets(
    principal: CurrentPrincipal,
    interactor: FromDishka[ListWalletsInteractor],
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> WalletPageResponse:
    page = await interactor(
        ListWalletsQuery(owner_id=principal.owner_id, limit=limit, cursor=cursor)
    )
    return WalletPageResponse(
        items=[WalletResponse.of(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{wallet_id}", response_model=WalletResponse)
async def get_wallet(
    wallet_id: UUID,
    principal: CurrentPrincipal,
    interactor: FromDishka[GetWalletInteractor],
) -> WalletResponse:
    wallet = await interactor(
        GetWalletQuery(wallet_id=WalletId(wallet_id), owner_id=principal.owner_id)
    )
    return WalletResponse.of(wallet)


@router.post("/{wallet_id}/deposits", response_model=MovementResponse)
async def deposit(
    wallet_id: UUID,
    payload: MoneyRequest,
    principal: CurrentPrincipal,
    interactor: FromDishka[DepositToWalletInteractor],
    idempotency_key: IdempotencyKey = None,
) -> MovementResponse:
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


@router.post("/{wallet_id}/withdrawals", response_model=MovementResponse)
async def withdraw(
    wallet_id: UUID,
    payload: MoneyRequest,
    principal: CurrentPrincipal,
    interactor: FromDishka[WithdrawFromWalletInteractor],
    idempotency_key: IdempotencyKey = None,
) -> MovementResponse:
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


@router.get("/{wallet_id}/entries", response_model=LedgerPageResponse)
async def list_entries(
    wallet_id: UUID,
    principal: CurrentPrincipal,
    interactor: FromDishka[ListLedgerEntriesInteractor],
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> LedgerPageResponse:
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
