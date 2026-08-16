from uuid import UUID

from evenkeel.application.errors import ApplicationError, ApplicationErrorCode
from evenkeel.application.ports.identity import IdentityProvider, Principal
from evenkeel.domain.value_objects.ids import OwnerId


class UnauthenticatedError(ApplicationError):
    default_status_code = 401


class DevIdentityProvider(IdentityProvider):
    """Development identity: the credential *is* the owner id.

    This exists so the template starts with no auth server, and it is why the
    server entrypoint refuses to boot in production while it is still wired in.
    Replace it with the JWT/JWKS adapter before exposing anything.
    """

    async def authenticate(self, credential: str | None) -> Principal:
        if not credential:
            raise UnauthenticatedError(
                ApplicationErrorCode.VALIDATION_FAILED,
                details={"authorization": "missing credential"},
            )
        try:
            return Principal(owner_id=OwnerId(UUID(credential)))
        except ValueError as exc:
            raise UnauthenticatedError(
                ApplicationErrorCode.VALIDATION_FAILED,
                details={"authorization": "credential must be a UUID owner id"},
            ) from exc
