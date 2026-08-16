from typing import Annotated

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from appcore.application.ports.identity import IdentityProvider, Principal

bearer_scheme = HTTPBearer(auto_error=False)


@inject
async def current_principal(
    identity: FromDishka[IdentityProvider],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> Principal:
    """Authenticate, and nothing else.

    No profile creation, no last-seen bump, no side effects: a dependency that
    writes turns every read endpoint into a write endpoint, which shows up as
    unexplained database load and as failures on a read-only replica.
    """
    return await identity.authenticate(credentials.credentials if credentials else None)


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
