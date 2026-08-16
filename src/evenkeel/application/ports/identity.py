from dataclasses import dataclass
from typing import Protocol

from evenkeel.domain.value_objects.ids import OwnerId


@dataclass(frozen=True, slots=True)
class Principal:
    owner_id: OwnerId


class IdentityProvider(Protocol):
    """Turns an opaque credential into a principal.

    The port takes a string rather than a request so authentication can be
    reused outside HTTP, and so the application layer never learns whether the
    credential arrived as a bearer token, a cookie or a header.
    """

    async def authenticate(self, credential: str | None) -> Principal: ...
