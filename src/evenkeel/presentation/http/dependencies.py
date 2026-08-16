from typing import Annotated

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from evenkeel.application.ports.identity import IdentityProvider, Principal

bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="Owner bearer token",
    bearerFormat="UUID",
    # This text is the answer to the first question anybody has about the API,
    # and the published reference is where they ask it. A security scheme with
    # no description renders as "no authentication selected" and leaves the
    # reader guessing what a token even is here.
    description=(
        "Any UUID, and it is the owner id — there is no signup, no login "
        "endpoint and no token service in this template.\n\n"
        "The bundled `DevIdentityProvider` treats the bearer token as the "
        "identity, so `Authorization: Bearer 018f4b2c-...` opens a fresh "
        "account and `uuidgen` is the whole onboarding flow. Wallets are "
        "scoped to it: use the same value again to see them, a different one "
        "to be somebody else.\n\n"
        "That adapter refuses to start outside a local run, which is enforced "
        "by the boot guard. A real deployment swaps one provider method for a "
        "JWT/JWKS adapter, and this description changes with it."
    ),
)


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
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description=(
            "Reuse the same value when retrying a request you are unsure "
            "completed. The movement is applied once and the retry returns the "
            "original entry with `replayed: true`; a new value moves money "
            "again.\n\n"
            "Scoped per owner, so two clients picking `1` do not collide. "
            "Reusing one with a different payload is refused with `409` rather "
            "than confirming an operation that never ran, and a duplicate that "
            "arrives while the first is still running is refused too — there is "
            "no result to replay yet."
        ),
        examples=["7d1f8e0a-4b3c-4e2a-9f1d-2c5b6a7e8d90"],
    ),
]
