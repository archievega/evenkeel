"""JWKS fetching, async and bounded.

PyJWT's `PyJWKClient` is synchronous — `urllib` on the request path — and it
re-fetches on every unknown `kid`, which anyone can mint. Parsing and crypto
stay PyJWT's; the fetch, the refresh floor and the lock are ours.
"""

import asyncio
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiohttp
from jwt import PyJWK, PyJWKSet
from jwt.exceptions import PyJWTError

from evenkeel.application.errors import (
    ApplicationErrorCode,
    DependencyUnavailableError,
)
from evenkeel.infrastructure.adapters.http.transport import decode_object, read_capped
from evenkeel.logging import get_logger

log = get_logger(__name__)

_MAX_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class JwksPolicy:
    url: str
    ttl_seconds: float = 600.0
    # The floor on refresh attempts, and the only reason this is not
    # `PyJWKClient`: an unknown `kid` triggers a fetch, and a token naming a key
    # id that does not exist costs an attacker nothing to produce.
    min_refresh_seconds: float = 30.0
    # How long keys may outlive their TTL while the provider is unreachable.
    max_stale_seconds: float = 3600.0


class _Unusable(Exception):
    """A key set that arrived but cannot be used. Never leaves this module."""


@asynccontextmanager
async def _translating_jwks_errors() -> AsyncIterator[None]:
    """Anything that goes wrong reaching or reading the key set is one 503.

    Same reason as `translating_redis_errors`: a caller handling an outage
    should not have to know that `aiohttp`, `json` and PyJWT each have their own
    way of saying the dependency did not answer usefully. The reason is logged,
    never returned.
    """
    try:
        yield
    except (_Unusable, TimeoutError, aiohttp.ClientError, PyJWTError) as exc:
        log.warning(
            "jwks_unavailable", error_type=type(exc).__name__, reason=str(exc)[:200]
        )
        raise DependencyUnavailableError(
            ApplicationErrorCode.DEPENDENCY_UNAVAILABLE, details={"dependency": "jwks"}
        ) from exc


class JwksCache:
    def __init__(self, session: aiohttp.ClientSession, policy: JwksPolicy) -> None:
        self._session = session
        self._policy = policy
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = -math.inf
        self._last_attempt = -math.inf
        self._lock = asyncio.Lock()

    async def get(self, kid: str) -> PyJWK | None:
        """The signing key, or `None` if this key set does not have it.

        `None` rather than a 401: a key cache that refuses credentials has
        opinions about a layer above it.
        """
        key = self._keys.get(kid)
        if key is not None and (not self._stale() or self._lock.locked()):
            # A refresh already in flight is not a reason to queue behind it
            # holding a key that verifies. Without this, one slow fetch stalls
            # every authentication for as long as it runs.
            return key

        async with self._lock:
            # Re-checked: whoever held the lock may have just fetched this key.
            key = self._keys.get(kid)
            if key is not None and not self._stale():
                return key
            if time.monotonic() - self._last_attempt < self._policy.min_refresh_seconds:
                return key

            try:
                await self._refresh()
            except DependencyUnavailableError:
                if key is None or self._age() >= self._policy.max_stale_seconds:
                    raise
                # Expiry is a rotation hint, not a revocation — up to a ceiling,
                # past which a key nobody has been able to confirm stops being
                # an answer.
                log.warning("jwks_serving_stale_keys")
                return key
            return self._keys.get(kid)

    def _age(self) -> float:
        return time.monotonic() - self._fetched_at

    def _stale(self) -> bool:
        return self._age() >= self._policy.ttl_seconds

    async def _refresh(self) -> None:
        self._last_attempt = time.monotonic()
        async with _translating_jwks_errors():
            async with self._session.get(
                self._policy.url,
                # Not followed, and this is the one place it matters most: the
                # answer to this request is the set of keys that decide who
                # every caller is. A `302` from the issuer's endpoint would let
                # whoever wrote the `Location` supply them.
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise _Unusable(f"responded {response.status}")
                raw = await read_capped(response.content, _MAX_BYTES + 1)
                if len(raw) > _MAX_BYTES:
                    raise _Unusable("implausibly large")

            document = decode_object(raw)
            entries = document.get("keys") if document else None
            if not isinstance(entries, list):
                raise _Unusable("not a key set")

            keys: dict[str, PyJWK] = {}
            # An empty list is the issuer revoking everything, and it is an
            # answer rather than an outage: raising would send it to the
            # stale-key path above, where the withdrawn keys would keep working.
            # PyJWKSet drops entries it cannot build and keys published for
            # encryption rather than signatures, which is the RFC 7517 `use`
            # rule and the kind of thing worth not rewriting.
            for key in PyJWKSet(entries).keys if entries else []:
                # First wins: last-wins would let one appended duplicate
                # blackhole a live key id, and valid tokens would just stop.
                if key.key_id and key.key_id not in keys:
                    keys[key.key_id] = key

        self._keys = keys
        self._fetched_at = time.monotonic()
        log.info("jwks_refreshed", keys=len(keys))
