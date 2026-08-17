"""Token verification, against a real key set on a real socket.

Not mocked, for the same reason as `test_outbound_transport.py`: the failures
worth catching here are ones a mock cannot have. One of them — a key set
arriving in more than one TCP segment — is invisible to every test that does not
serve the document in pieces.

One caveat, stated rather than implied. `test_an_alg_none_token_is_refused` and
its HMAC sibling pass even against `algorithms=[header["alg"]]`, because PyJWT
2.13 refuses a header algorithm that disagrees with the JWKS key's own. They are
regression tests for the floor pinned in `pyproject.toml`. The adapter's own
property — that the allowlist comes from configuration — is
`test_an_algorithm_outside_the_configured_allowlist_is_refused`.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

pytest.importorskip("jwt", reason="requires the `jwt` extra")

import aiohttp
import jwt
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import rsa

from evenkeel.application.errors import (
    DependencyUnavailableError,
    UnauthenticatedError,
)
from evenkeel.infrastructure.adapters.http.transport import SessionPolicy, open_session
from evenkeel.infrastructure.adapters.jwt.identity import (
    JwtIdentityProvider,
    JwtPolicy,
)
from evenkeel.infrastructure.adapters.jwt.keys import (
    _MAX_BYTES,
    JwksCache,
    JwksPolicy,
)

ISSUER = "https://issuer.example"
AUDIENCE = "evenkeel"
KID = "test-key-1"
POLICY = JwtPolicy(issuer=ISSUER, audience=AUDIENCE, algorithms=("RS256",))


def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa_key()


def as_jwk(key: rsa.RSAPrivateKey, *, kid: str) -> dict[str, object]:
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**public, "kid": kid, "use": "sig", "alg": "RS256"}


@pytest.fixture(scope="module")
def jwks(signing_key: rsa.RSAPrivateKey) -> dict[str, object]:
    return {"keys": [as_jwk(signing_key, kid=KID)]}


def token(
    signing_key: rsa.RSAPrivateKey, *, kid: str | None = KID, **claims: object
) -> str:
    payload: dict[str, object] = {
        "sub": str(uuid4()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    payload.update(claims)
    return jwt.encode(
        {k: v for k, v in payload.items() if v is not None},
        signing_key,
        algorithm="RS256",
        headers={"kid": kid} if kid else {},
    )


def forge_hs256(claims: dict[str, object], *, secret: str) -> str:
    def segment(data: object) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=")

    signing_input = b".".join(
        (segment({"alg": "HS256", "typ": "JWT", "kid": KID}), segment(claims))
    )
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return (
        signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")
    ).decode()


class Served:
    """A JWKS endpoint with dials, including a segment size — the thing a
    loopback test otherwise never varies."""

    def __init__(self, document: dict[str, object]) -> None:
        self.document = document
        self.requests = 0
        self.delay = 0.0
        self.status = 200
        self.raw: bytes | None = None
        self.segment_bytes = 0
        self.segment_delay = 0.01
        self.endless = False


@asynccontextmanager
async def serving(document: dict[str, object]) -> AsyncIterator[tuple[str, Served]]:
    state = Served(document)

    async def handler(request: web.Request) -> web.StreamResponse:
        state.requests += 1
        if state.delay:
            await asyncio.sleep(state.delay)
        if state.endless:
            response = web.StreamResponse(status=200)
            response.content_type = "application/json"
            await response.prepare(request)
            try:
                while True:
                    await response.write(b"x" * 8192)
            except (ConnectionResetError, aiohttp.ClientConnectionError):
                return response
        if state.raw is not None:
            return web.Response(body=state.raw, content_type="text/html")
        if not state.segment_bytes:
            return web.json_response(state.document, status=state.status)

        body = json.dumps(state.document).encode()
        response = web.StreamResponse(status=state.status)
        response.content_type = "application/json"
        await response.prepare(request)
        for start in range(0, len(body), state.segment_bytes):
            await response.write(body[start : start + state.segment_bytes])
            await asyncio.sleep(state.segment_delay)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/jwks.json", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}/jwks.json", state
    finally:
        await runner.cleanup()


@asynccontextmanager
async def provider(
    url: str,
    *,
    keys: JwksPolicy | None = None,
    policy: JwtPolicy = POLICY,
    session_ms: int = 5_000,
) -> AsyncIterator[JwtIdentityProvider]:
    async with open_session(
        SessionPolicy(backstop_timeout_ms=session_ms), headers={}
    ) as session:
        yield JwtIdentityProvider(
            JwksCache(session, keys or JwksPolicy(url=url, min_refresh_seconds=0.0)),
            policy,
        )


@pytest.mark.cwe(347)
async def test_an_algorithm_outside_the_configured_allowlist_is_refused(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """The adapter's own property: the allowlist is configuration.

    A valid RS256 token, signed by the key the issuer published, is refused when
    the deployment says it accepts only RS512. Nothing about the token changes —
    only the policy — so this fails the moment the algorithm starts coming from
    anywhere else.
    """
    strict = JwtPolicy(issuer=ISSUER, audience=AUDIENCE, algorithms=("RS512",))

    async with serving(jwks) as (url, _), provider(url, policy=strict) as identity:
        with pytest.raises(UnauthenticatedError):
            await identity.authenticate(token(signing_key))


@pytest.mark.cwe(347)
async def test_the_verifier_never_offers_an_algorithm_policy_did_not_name(
    signing_key: rsa.RSAPrivateKey,
    jwks: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the adapter *hands* PyJWT, not what PyJWT does with it.

    This replaces two tests named for `alg: none` and HMAC-with-the-public-key.
    Both were green against `algorithms=[header["alg"]]`, and green again against
    the adapter explicitly allowlisting `none` and `HS256` — because PyJWT 2.13
    refuses a header algorithm that disagrees with the JWKS key, so the library
    was doing all the work and the tests were testing the library.

    Recording the argument is the only assertion that cannot be satisfied by
    somebody else's defence. The forged token is what makes the second call's
    header disagree with policy, which is why one token is not enough.
    """
    offered: list[list[str]] = []
    real = jwt.decode

    def recording(*args: object, **kwargs: object) -> object:
        offered.append(list(kwargs["algorithms"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(jwt, "decode", recording)
    forged = forge_hs256(
        {"sub": str(uuid4()), "iss": ISSUER, "aud": AUDIENCE, "exp": time.time() + 60},
        secret="whatever",
    )

    async with serving(jwks) as (url, _), provider(url) as identity:
        await identity.authenticate(token(signing_key))
        with pytest.raises(UnauthenticatedError):
            await identity.authenticate(forged)

    assert offered == [["RS256"], ["RS256"]]


@pytest.mark.cwe(347)
async def test_a_token_signed_by_a_different_key_is_refused(
    jwks: dict[str, object],
) -> None:
    async with serving(jwks) as (url, _), provider(url) as identity:
        with pytest.raises(UnauthenticatedError):
            await identity.authenticate(token(rsa_key()))


@pytest.mark.cwe(306)
async def test_a_valid_token_becomes_a_principal(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    owner = uuid4()

    async with serving(jwks) as (url, _), provider(url) as identity:
        principal = await identity.authenticate(token(signing_key, sub=str(owner)))

    assert principal.owner_id.value == owner


@pytest.mark.cwe(306)
@pytest.mark.parametrize(
    ("claims", "why"),
    [
        ({"exp": int(time.time()) - 600}, "expired"),
        ({"iss": "https://somebody.else"}, "wrong issuer"),
        ({"aud": "another-service"}, "wrong audience"),
        ({"nbf": int(time.time()) + 600}, "not valid yet"),
        ({"exp": None}, "no expiry at all"),
        ({"iss": None}, "no issuer"),
        ({"sub": None}, "no subject"),
    ],
)
async def test_a_token_that_fails_a_claim_is_refused(
    signing_key: rsa.RSAPrivateKey,
    jwks: dict[str, object],
    claims: dict[str, object],
    why: str,
) -> None:
    """`{"exp": None}` is the case the `require` list carries: without it, a
    token with no `exp` never expires. The other two absences are refused by
    PyJWT's `issuer=` argument and by `_owner_id`, so they hold either way.
    """
    async with serving(jwks) as (url, _), provider(url) as identity:
        with pytest.raises(UnauthenticatedError):
            await identity.authenticate(token(signing_key, **claims))


@pytest.mark.cwe(755)
async def test_a_claim_of_the_wrong_type_is_refused_rather_than_crashing(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """PyJWT calls `int()` on `exp`, and a JSON object raises `TypeError`, which
    is not under `PyJWTError`. Uncaught, a non-conformant issuer becomes a 500
    with a stack trace."""
    async with serving(jwks) as (url, _), provider(url) as identity:
        for broken in ({"at": "noon"}, ["soon"]):
            with pytest.raises(UnauthenticatedError):
                await identity.authenticate(token(signing_key, exp=broken))


# Expired against a non-canonical `sub`, not against a bad signature: both go
# through `_refused`, which is the regression this guards.
@pytest.mark.cwe(209)
async def test_the_refusal_never_says_which_check_failed(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    async with serving(jwks) as (url, _), provider(url) as identity:
        with pytest.raises(UnauthenticatedError) as expired:
            await identity.authenticate(token(signing_key, exp=int(time.time()) - 600))
        with pytest.raises(UnauthenticatedError) as forged:
            await identity.authenticate(token(signing_key, sub="not-a-uuid"))

    assert expired.value.details == forged.value.details
    assert str(expired.value) == str(forged.value)


@pytest.mark.cwe(306)
@pytest.mark.parametrize(
    "subject",
    [
        "alice@example.com",
        "6A3514EB-1561-4E76-A51E-BED709FF0544",
        "6a3514eb15614e76a51ebed709ff0544",
        "urn:uuid:6a3514eb-1561-4e76-a51e-bed709ff0544",
        "{6a3514eb-1561-4e76-a51e-bed709ff0544}",
    ],
)
async def test_a_subject_that_is_not_a_canonical_uuid_is_refused(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object], subject: str
) -> None:
    """`UUID()` is a parser, not a validator: the last four parse to one value.
    `sub` is opaque to the issuer, so accepting them would merge subjects it
    considers distinct into one owner holding one set of wallets."""
    async with serving(jwks) as (url, _), provider(url) as identity:
        with pytest.raises(UnauthenticatedError):
            await identity.authenticate(token(signing_key, sub=subject))


@pytest.mark.cwe(306)
async def test_nothing_is_accepted_without_a_credential(
    jwks: dict[str, object],
) -> None:
    """The `requests == 0` half cannot fail — no credential means no `kid` on
    any path — and is kept only as a statement of the obvious next question."""
    async with serving(jwks) as (url, served), provider(url) as identity:
        for empty in (None, ""):
            with pytest.raises(UnauthenticatedError):
                await identity.authenticate(empty)

    assert served.requests == 0


@pytest.mark.cwe(347)
async def test_a_token_without_a_key_id_is_refused_without_a_fetch(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    async with serving(jwks) as (url, served), provider(url) as identity:
        with pytest.raises(UnauthenticatedError):
            await identity.authenticate(token(signing_key, kid=None))

    assert served.requests == 0


@pytest.mark.cwe(770)
async def test_the_key_set_is_fetched_once_for_many_tokens(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    async with serving(jwks) as (url, served), provider(url) as identity:
        for _ in range(20):
            await identity.authenticate(token(signing_key))

    assert served.requests == 1


@pytest.mark.cwe(770)
async def test_a_cold_burst_does_not_stampede_the_provider(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """The delay is what makes the race real rather than theoretical."""
    async with serving(jwks) as (url, served), provider(url) as identity:
        served.delay = 0.05
        await asyncio.gather(
            *(identity.authenticate(token(signing_key)) for _ in range(50))
        )

    assert served.requests == 1


@pytest.mark.cwe(770)
async def test_unknown_key_ids_do_not_become_a_fetch_each(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """Anyone can mint a token naming a key id that does not exist."""
    async with (
        serving(jwks) as (url, served),
        provider(url, keys=JwksPolicy(url=url, min_refresh_seconds=60.0)) as identity,
    ):
        for _ in range(10):
            with pytest.raises(UnauthenticatedError):
                await identity.authenticate(token(signing_key, kid="who-knows"))

    assert served.requests == 1


@pytest.mark.cwe(770)
async def test_a_slow_refresh_does_not_stall_tokens_whose_key_is_in_memory(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """Once the TTL lapses, waiting on the refresh puts the provider's latency
    in front of every authentication — including the ones whose key is already
    loaded and still verifies."""
    async with (
        serving(jwks) as (url, served),
        provider(
            url, keys=JwksPolicy(url=url, ttl_seconds=0.0, min_refresh_seconds=0.0)
        ) as identity,
    ):
        await identity.authenticate(token(signing_key))
        served.delay = 0.5

        started = time.perf_counter()
        await asyncio.gather(
            *(identity.authenticate(token(signing_key)) for _ in range(20))
        )
        elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"{elapsed:.2f}s — the holders queued behind the fetch"
    assert served.requests == 2


@pytest.mark.cwe(755)
async def test_an_unreachable_key_set_is_a_dependency_failure(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """503, not 401: the token may be fine and this service cannot tell."""
    async with provider("http://127.0.0.1:1/jwks.json") as identity:
        with pytest.raises(DependencyUnavailableError):
            await identity.authenticate(token(signing_key))


@pytest.mark.cwe(755)
async def test_a_key_set_that_is_not_json_is_a_dependency_failure(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """A provider behind a captive portal answers 200 with HTML."""
    async with serving(jwks) as (url, served), provider(url) as identity:
        served.raw = b"<html>sign in to the wifi</html>"
        with pytest.raises(DependencyUnavailableError):
            await identity.authenticate(token(signing_key))


@pytest.mark.cwe(400)
async def test_an_implausibly_large_key_set_is_refused(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """Endless rather than merely large: a body with a known size proves the
    length check fires, and the point of the cap is that the read stops.

    Termination is all this proves — it passes with `_MAX_BYTES` at 256 MB. The
    bound itself is `test_the_key_set_cap_is_a_size_somebody_chose` below.
    """
    async with serving(jwks) as (url, served), provider(url) as identity:
        served.endless = True
        with pytest.raises(DependencyUnavailableError):
            await asyncio.wait_for(identity.authenticate(token(signing_key)), timeout=5)


@pytest.mark.cwe(755)
async def test_one_unusable_key_does_not_discard_the_rest(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    mixed = {"keys": [{"kty": "nonsense", "kid": "broken"}, *jwks["keys"]]}  # type: ignore[misc]

    async with serving(mixed) as (url, _), provider(url) as identity:
        principal = await identity.authenticate(token(signing_key))

    assert principal.owner_id is not None


@pytest.mark.cwe(613)
async def test_an_empty_key_set_revokes_rather_than_reading_as_an_outage(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """Publishing `{"keys": []}` is how an issuer revokes everything at once.

    Read as a failure it falls through to the stale-key path, and the keys the
    issuer just withdrew keep verifying tokens indefinitely.
    """
    async with (
        serving(jwks) as (url, served),
        provider(
            url, keys=JwksPolicy(url=url, ttl_seconds=0.0, min_refresh_seconds=0.0)
        ) as identity,
    ):
        await identity.authenticate(token(signing_key))
        served.document = {"keys": []}

        with pytest.raises(UnauthenticatedError):
            await identity.authenticate(token(signing_key))


@pytest.mark.cwe(613)
async def test_a_duplicate_key_id_cannot_displace_the_live_key(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """Last-wins would let one appended entry blackhole a live key id, and the
    only symptom is that valid tokens stop verifying."""
    shadowed = {"keys": [as_jwk(signing_key, kid=KID), as_jwk(rsa_key(), kid=KID)]}

    async with serving(shadowed) as (url, _), provider(url) as identity:
        principal = await identity.authenticate(token(signing_key))

    assert principal.owner_id is not None


@pytest.mark.cwe(755)
async def test_keys_already_held_survive_the_provider_going_down(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """Expiry is a rotation hint, not a revocation. Refusing everything while
    the provider is down would hand it this service's availability."""
    async with (
        serving(jwks) as (url, served),
        provider(
            url, keys=JwksPolicy(url=url, ttl_seconds=0.0, min_refresh_seconds=0.0)
        ) as identity,
    ):
        await identity.authenticate(token(signing_key))
        served.status = 500

        principal = await identity.authenticate(token(signing_key))

    assert principal.owner_id is not None
    assert served.requests == 2, "the refresh was attempted, and then survived"


@pytest.mark.cwe(613)
async def test_stale_keys_stop_being_an_answer_past_the_grace_ceiling(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """Unbounded, the fallback above means a key withdrawn during an outage
    keeps working for as long as the outage lasts."""
    async with (
        serving(jwks) as (url, served),
        provider(
            url,
            keys=JwksPolicy(
                url=url,
                ttl_seconds=0.0,
                min_refresh_seconds=0.0,
                max_stale_seconds=0.0,
            ),
        ) as identity,
    ):
        await identity.authenticate(token(signing_key))
        served.status = 500

        with pytest.raises(DependencyUnavailableError):
            await identity.authenticate(token(signing_key))


@pytest.mark.cwe(918)
async def test_the_key_set_is_not_fetched_through_a_redirect(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """The answer to this request decides who every caller is.

    `aiohttp` follows redirects by default and across hosts, so before the fix a
    `302` from the issuer's endpoint handed the signing keys to whoever wrote
    the `Location` — and this test watched an attacker-supplied key be returned
    and used.
    """
    attacker = {"keys": [as_jwk(rsa_key(), kid=KID)]}

    async with serving(attacker) as (attacker_url, attacker_state):

        async def redirecting(request: web.Request) -> web.StreamResponse:
            raise web.HTTPFound(attacker_url)

        app = web.Application()
        app.router.add_get("/jwks.json", redirecting)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        issuer_url = f"http://127.0.0.1:{runner.addresses[0][1]}/jwks.json"

        try:
            async with provider(issuer_url) as identity:
                with pytest.raises(DependencyUnavailableError):
                    await identity.authenticate(token(signing_key))
        finally:
            await runner.cleanup()

    assert attacker_state.requests == 0, "the redirect target must never be read"


@pytest.mark.cwe(770)
async def test_a_dribbling_key_set_cannot_hold_the_refresh_open(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """One byte every 200ms is a live connection making progress, so nothing
    below the fetch's own timeout stops it.

    `JwksPolicy.timeout_ms` is that timeout; left to the session backstop it was
    five seconds. The lock is not asserted here — there is one caller — so this
    bounds the fetch, and `test_a_slow_refresh_does_not_stall_tokens...` is what
    covers the queue behind it.
    """
    async with (
        serving(jwks) as (url, served),
        provider(url, keys=JwksPolicy(url=url, timeout_ms=1_500)) as identity,
    ):
        served.segment_bytes = 1
        served.segment_delay = 0.2

        started = time.perf_counter()
        with pytest.raises(DependencyUnavailableError):
            await asyncio.wait_for(identity.authenticate(token(signing_key)), timeout=10)
        elapsed = time.perf_counter() - started

    assert elapsed < 5, f"{elapsed:.1f}s — the drip was not bounded"


@pytest.mark.cwe(347)
async def test_a_key_published_for_encryption_cannot_verify_a_signature(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """RFC 7517 §4.2. `PyJWKSet` does not read `use` — `PyJWKClient` does, and
    that is the class this adapter replaces, so the filter has to be here. The
    docstring claimed PyJWT did it; it did not, and an issuer publishing its
    encryption key in the same document had it verifying tokens."""
    document = {"keys": [{**as_jwk(signing_key, kid=KID), "use": "enc"}]}

    async with serving(document) as (url, _), provider(url) as identity:
        with pytest.raises(UnauthenticatedError):
            await identity.authenticate(token(signing_key))


@pytest.mark.cwe(755)
@pytest.mark.parametrize("junk", [1, "a string", ["nested"], None, True])
async def test_a_key_set_entry_that_is_not_an_object_is_a_503_not_a_crash(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object], junk: object
) -> None:
    """`PyJWKSet` calls `.get` on each entry, so a scalar raised `AttributeError`
    straight past the error translation and answered 500 — and one junk entry
    stopped the whole set from ever loading."""
    document = {"keys": [junk, *jwks["keys"]]}  # type: ignore[misc]

    async with serving(document) as (url, _), provider(url) as identity:
        principal = await identity.authenticate(token(signing_key))

    assert principal.owner_id is not None, "the usable key must survive the junk"


@pytest.mark.cwe(613)
async def test_the_grace_ceiling_holds_inside_the_refresh_floor(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """The floor and the ceiling are different limits and used to fight.

    An early return for "refreshed too recently" handed back the cached key
    without consulting the ceiling, so for the whole floor window — 30 seconds
    by default — a key past its grace kept verifying tokens.
    """
    async with (
        serving(jwks) as (url, served),
        provider(
            url,
            keys=JwksPolicy(
                url=url,
                ttl_seconds=0.0,
                min_refresh_seconds=0.5,
                max_stale_seconds=0.0,
            ),
        ) as identity,
    ):
        await identity.authenticate(token(signing_key))
        served.status = 500
        await asyncio.sleep(0.6)

        # Past the floor: this one attempts, fails, and is beyond the ceiling.
        with pytest.raises(DependencyUnavailableError):
            await identity.authenticate(token(signing_key))

        # Inside the floor: no attempt is made, and the ceiling must still hold.
        with pytest.raises(DependencyUnavailableError):
            await identity.authenticate(token(signing_key))

    assert served.requests == 2, "the second refusal came from the floor, not a fetch"


@pytest.mark.cwe(755)
async def test_an_outage_inside_the_refresh_floor_is_a_503_not_a_401(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """A client told `401` clears the session and logs the user out. A client
    told `503` retries. The distinction is the provider's last word, and inside
    the floor the code used to discard it."""
    async with (
        serving(jwks) as (url, served),
        provider(
            url,
            keys=JwksPolicy(url=url, ttl_seconds=0.0, min_refresh_seconds=60.0),
        ) as identity,
    ):
        served.status = 500
        with pytest.raises(DependencyUnavailableError):
            await identity.authenticate(token(signing_key))
        with pytest.raises(DependencyUnavailableError):
            await identity.authenticate(token(signing_key, kid="rotated"))

    assert served.requests == 1


@pytest.mark.cwe(755)
async def test_a_non_numeric_expiry_is_refused_rather_than_crashing(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """`json` accepts the `Infinity` literal and `int()` answers `OverflowError`,
    which is not under `PyJWTError` and not a `ValueError`."""
    async with serving(jwks) as (url, _), provider(url) as identity:
        for broken in (float("inf"), float("-inf"), {"at": "noon"}, ["soon"]):
            with pytest.raises(UnauthenticatedError):
                await identity.authenticate(token(signing_key, exp=broken))


@pytest.mark.cwe(400)
def test_the_key_set_cap_is_a_size_somebody_chose() -> None:
    """The endless-body test above proves the read terminates and nothing more.

    It passes with the cap at 256 MB, which is a memory bound nobody signed off:
    against a concurrency cap of 32 that is eight gigabytes of key set. A JWKS
    is a handful of public keys.
    """
    assert _MAX_BYTES <= 512 * 1024


@pytest.mark.cwe(755)
async def test_a_key_set_of_nothing_usable_is_an_outage_not_a_revocation(
    signing_key: rsa.RSAPrivateKey, jwks: dict[str, object]
) -> None:
    """A decision, not a side effect, so it gets a test.

    Entries that parse as JSON but that no supported algorithm can build are
    read as "we do not understand this document" rather than "the issuer
    withdrew everything" — far more likely our library than their revocation,
    and a missing `cryptography` should not log every user out. The stale keys
    therefore keep serving until the grace ceiling. An explicitly empty list
    still revokes, which `test_an_empty_key_set_revokes...` covers.
    """
    async with (
        serving(jwks) as (url, served),
        provider(
            url, keys=JwksPolicy(url=url, ttl_seconds=0.0, min_refresh_seconds=0.0)
        ) as identity,
    ):
        await identity.authenticate(token(signing_key))
        served.document = {"keys": [{"kty": "AN-ALGORITHM-FROM-2035", "kid": KID}]}

        principal = await identity.authenticate(token(signing_key))

    assert principal.owner_id is not None
