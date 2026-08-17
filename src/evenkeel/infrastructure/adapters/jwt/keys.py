"""JWKS fetching, async and bounded.

`PyJWKClient` is synchronous — `urllib` on the request path — and re-fetches on
every unknown `kid`, which anyone can mint. Crypto stays PyJWT's; the fetch, the
refresh floor and the lock are ours.
"""

import asyncio
import math
import time
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
    # The floor, and the only reason this is not `PyJWKClient`: an unknown `kid`
    # triggers a fetch and costs an attacker nothing to mint.
    min_refresh_seconds: float = 30.0
    # How long keys may outlive their TTL while the provider is unreachable.
    max_stale_seconds: float = 3600.0
    timeout_ms: int = 5_000


def _unavailable(reason: str) -> DependencyUnavailableError:
    # The reason is logged, never returned.
    log.warning("jwks_unavailable", reason=reason)
    return DependencyUnavailableError(
        ApplicationErrorCode.DEPENDENCY_UNAVAILABLE, details={"dependency": "jwks"}
    )


class JwksCache:
    def __init__(self, session: aiohttp.ClientSession, policy: JwksPolicy) -> None:
        self._session = session
        self._policy = policy
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = -math.inf
        self._last_attempt = -math.inf
        self._last_attempt_failed = False
        self._lock = asyncio.Lock()

    async def get(self, kid: str) -> PyJWK | None:
        """The signing key, or `None` if this key set does not have it.

        `None` rather than a 401: a key cache does not decide about credentials.
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
                # Throttled, so nothing new is known — but the answer depends on
                # the last attempt. Provider answered: a missing `kid` is
                # definitive. Provider failed: this is an outage, and reporting a
                # bad token for a good one makes a client discard its session.
                if self._last_attempt_failed:
                    return self._serve_within_grace(key)
                return key

            try:
                await self._refresh()
            except DependencyUnavailableError:
                self._last_attempt_failed = True
                if key is None:
                    raise
                return self._serve_within_grace(key)
            self._last_attempt_failed = False
            return self._keys.get(kid)

    def _serve_within_grace(self, key: PyJWK | None) -> PyJWK:
        """A cached key while the provider's last word was a failure.

        Expiry is a rotation hint, not a revocation — up to a ceiling, past
        which a key nobody can confirm stops being an answer.
        """
        if key is None or self._age() >= self._policy.max_stale_seconds:
            raise _unavailable("no key inside the grace ceiling")
        log.warning("jwks_serving_stale_keys", age_seconds=round(self._age()))
        return key

    def _age(self) -> float:
        return time.monotonic() - self._fetched_at

    def _stale(self) -> bool:
        return self._age() >= self._policy.ttl_seconds

    async def _refresh(self) -> None:
        self._last_attempt = time.monotonic()
        try:
            self._keys = await self._fetch()
        except (TimeoutError, aiohttp.ClientError, PyJWTError, ValueError) as exc:
            # One `except` beside the raising, rather than the context-manager
            # idiom `translating_redis_errors` uses — that one earns its shape
            # with five importing modules; here it was all one function.
            raise _unavailable(f"{type(exc).__name__}: {str(exc)[:120]}") from exc
        self._fetched_at = time.monotonic()
        log.info("jwks_refreshed", keys=len(self._keys))

    async def _fetch(self) -> dict[str, PyJWK]:
        async with self._session.get(
            self._policy.url,
            timeout=aiohttp.ClientTimeout(total=self._policy.timeout_ms / 1000),
            # Not followed, and this is the one place it matters most: the answer
            # to this request is the set of keys that decide who every caller is.
            # A `302` would let whoever wrote the `Location` supply them.
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ValueError(f"responded {response.status}")
            raw = await read_capped(response.content, _MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError("implausibly large")

        document = decode_object(raw)
        entries = document.get("keys") if document else None
        if not isinstance(entries, list):
            raise ValueError("not a key set")

        # An empty list is the issuer revoking everything — an answer, not an
        # outage, or the stale-key path above would keep serving withdrawn keys.
        #
        # `use` is filtered here because `PyJWKSet` never reads it (RFC 7517
        # §4.2), and `isinstance` because it calls `.get` on every entry.
        signing = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("use") in (None, "sig")
        ]
        keys: dict[str, PyJWK] = {}
        for key in PyJWKSet(signing).keys if signing else []:
            # First wins: last-wins lets one appended duplicate blackhole a live
            # key id, and valid tokens just stop.
            if key.key_id and key.key_id not in keys:
                keys[key.key_id] = key
        return keys
