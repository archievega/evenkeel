"""Verify tokens somebody else issued. Issue none.

Why there is no login endpoint: `docs/adr/0009-a-resource-server-not-an-auth-server.md`.
PyJWT does the crypto and the claims; what is here is which claim is the owner.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

import jwt

from evenkeel.application.errors import ApplicationErrorCode, UnauthenticatedError
from evenkeel.application.ports.identity import IdentityProvider, Principal
from evenkeel.domain.value_objects.ids import OwnerId
from evenkeel.infrastructure.adapters.http.transport import SessionPolicy, open_session
from evenkeel.infrastructure.adapters.jwt.keys import JwksCache, JwksPolicy
from evenkeel.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class JwtPolicy:
    """`algorithms` is an allowlist, never the token's own `alg`. `audience`
    keeps a sibling service's token out."""

    issuer: str
    audience: str
    algorithms: tuple[str, ...]
    owner_claim: str = "sub"
    # Clock skew, applied to `exp`, `nbf` and `iat` alike.
    leeway_seconds: float = 30.0


class JwtIdentityProvider(IdentityProvider):
    def __init__(self, keys: JwksCache, policy: JwtPolicy) -> None:
        self._keys = keys
        self._policy = policy

    async def authenticate(self, credential: str | None) -> Principal:
        if not credential:
            raise _refused("no credential")

        try:
            header = jwt.get_unverified_header(credential)
        except jwt.PyJWTError:
            raise _refused("unparseable token") from None

        # Only `kid` comes from the attacker-controlled header; the algorithm
        # comes from policy. `kid` is optional in RFC 7515 and required here:
        # a key set holds several keys through any rotation, and picking one for
        # a token that did not name one is guessing.
        kid = header.get("kid")
        key = await self._keys.get(kid) if isinstance(kid, str) and kid else None
        if key is None:
            raise _refused("unknown key id")

        try:
            claims = jwt.decode(
                credential,
                key=key,
                algorithms=list(self._policy.algorithms),
                issuer=self._policy.issuer,
                audience=self._policy.audience,
                leeway=self._policy.leeway_seconds,
                options={
                    "verify_signature": True,
                    # The rest of PyJWT's verify_* flags are already on by
                    # default; this list is not, and without it a token with no
                    # `exp` never expires.
                    "require": ["exp", "iss", "aud", self._policy.owner_claim],
                },
            )
        except (jwt.PyJWTError, TypeError, ValueError, ArithmeticError) as exc:
            # The non-PyJWT classes are deliberate: its validators call `int()`
            # on `exp`, so an object raises past `PyJWTError` and `Infinity` —
            # which `json` accepts — raises `OverflowError`. Both would 500.
            # Which check failed is never returned: "expired" versus "bad
            # signature" tells a forger the signature worked.
            raise _refused(type(exc).__name__) from None

        return Principal(owner_id=_owner_id(claims.get(self._policy.owner_claim)))


def _owner_id(subject: object) -> OwnerId:
    """The owner claim as an `OwnerId`, refused rather than derived.

    Canonical form required: `UUID()` is a parser, reading `{X}`, `urn:uuid:X`
    and an unhyphenated `X` as one value — so two subjects the issuer calls
    distinct would share wallets.
    """
    text = str(subject)
    try:
        parsed = UUID(text)
    except ValueError:
        raise _refused("owner claim is not a uuid") from None
    if str(parsed) != text:
        raise _refused("owner claim is not canonical") from None
    return OwnerId(parsed)


def _refused(reason: str) -> UnauthenticatedError:
    log.info("authentication_refused", reason=reason)
    return UnauthenticatedError(ApplicationErrorCode.UNAUTHENTICATED)


@asynccontextmanager
async def open_jwt_identity(
    *,
    jwt_policy: JwtPolicy,
    jwks_policy: JwksPolicy,
    session_policy: SessionPolicy,
) -> AsyncIterator[IdentityProvider]:
    """Its own session: a slow risk provider must not consume the connections
    that verify tokens."""
    async with open_session(session_policy, headers={}) as session:
        yield JwtIdentityProvider(JwksCache(session, jwks_policy), jwt_policy)
