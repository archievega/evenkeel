from uuid import UUID

from evenkeel.application.errors import (
    ApplicationErrorCode,
    UnauthenticatedError,
)
from evenkeel.application.ports.identity import IdentityProvider, Principal
from evenkeel.domain.value_objects.ids import OwnerId


class DevIdentityProvider(IdentityProvider):
    """Development identity: the credential *is* the owner id.

    This exists so the template starts with no auth server. The entrypoint
    refuses to boot while it is selected outside a `local` run — the guard
    matches on `app.identity_provider`, which is why that setting exists at all
    despite having only one value today. Replace it with a JWT/JWKS adapter
    before exposing anything.
    """

    async def authenticate(self, credential: str | None) -> Principal:
        if not credential:
            raise UnauthenticatedError(
                ApplicationErrorCode.UNAUTHENTICATED,
                details={"authorization": "missing credential"},
            )
        try:
            return Principal(owner_id=OwnerId(UUID(credential)))
        except ValueError as exc:
            raise UnauthenticatedError(
                ApplicationErrorCode.UNAUTHENTICATED,
                details={"authorization": "credential must be a UUID owner id"},
            ) from exc
