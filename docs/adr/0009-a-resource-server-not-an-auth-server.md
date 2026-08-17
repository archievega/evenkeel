# 9. Verify tokens, do not issue them

Accepted.

## Context

Until now the only identity adapter was `DevIdentityProvider`, which reads the
bearer token as the owner id. Anyone can be anyone. The boot guard refuses it
outside a local run, so the template could not be deployed — it could only be
read, and the README had to describe authentication as missing.

The obvious next step is "add auth", and the obvious reading of that is a login
endpoint, a password hash, an access token and a refresh token. That is the
wrong build for this service, and the reason is worth writing down because the
mistake is common: an access/refresh pair exists so that a short-lived token can
be renewed without re-authenticating **against a separate authorisation
server**. When the same process issues and accepts the token, the pair buys
nothing that a session cookie does not, and costs a rotation protocol, a
revocation store, and a login surface to attack.

A second temptation follows it: checking a revocation list on every request.
That undoes the one property a signed token has — that verifying it needs
nothing but a public key — and puts a database round trip on every authenticated
call, which is a query per request forever in exchange for shortening a window
that a short `exp` already bounds.

## Decision

This service is a **resource server**. It verifies tokens issued elsewhere and
issues none.

There is no login endpoint, no password hashing, no refresh rotation, no session
store and no per-request revocation lookup. `APP__APP__IDENTITY_PROVIDER=jwt`
turns on `JwtIdentityProvider`, which validates a bearer token against the
issuer's JWKS and produces a `Principal` — the same `Principal` the dev adapter
produces, so nothing above `infrastructure` changes.

PyJWT does the cryptography and the claim validation. What is ours is the part
PyJWT does not cover well for an async service: `PyJWKClient` fetches the key
set with `urllib`, synchronously, which on a request path blocks the event loop
for an HTTP round trip. So `JwksCache` fetches it with `aiohttp`, behind a
single lock, with a TTL and a floor on refresh attempts.

Three decisions inside that are not obvious:

**The algorithm comes from configuration.** Reading `alg` from the token is the
classic way a verifier accepts `none`, and accepts HMAC signed with the public
RSA key as the shared secret.

Worth being precise about where that defence actually lives, because the first
draft of this file got it wrong: on PyJWT 2.13 the library itself refuses a
header algorithm that disagrees with the JWKS key's own, so the two famous
attacks are stopped there rather than by our allowlist. The allowlist is still
load-bearing — it is what a deployment sets when its issuer signs with `ES256`,
and it is what would stop the attacks on an older PyJWT — so `pyproject.toml`
pins `>=2.13` and the test that proves the allowlist is honoured configures
`RS512` and watches a valid `RS256` token be refused.

**An unknown `kid` may trigger a refresh, but not one per token.** Anyone can
mint a token naming a key id that does not exist. Without
`JwksPolicy.min_refresh_seconds`, an unauthenticated caller turns this service into a
load generator aimed at the identity provider.

**Keys already held outlive the provider's outage, up to a ceiling.** A stale
key set still verifies a token signed with a key in it, and refusing everything
while the provider is down would hand it this service's availability. TTL expiry
is a rotation hint, not a revocation — but unbounded that reasoning means a key
withdrawn during an outage keeps working for as long as the outage lasts, so
`max_stale_seconds` ends it. An empty key set is treated as an answer rather
than a failure for the same reason: publishing `{"keys": []}` is how an issuer
revokes everything at once, and reading it as an outage would serve the
withdrawn keys from cache.

## Consequences

The template can be deployed for real. Point it at an OIDC provider whose `sub`
is a canonical UUID — Keycloak and Cognito are — and it authenticates against it
with no code change. Auth0 is not: its `sub` is `auth0|abc123`, so it needs
`APP__IDENTITY__OWNER_CLAIM` pointed at a claim that holds a UUID, and if the
tenant has none, a mapping this service does not have.

Revocation is bounded by `exp` and nothing else. A token stolen with fifteen
minutes left works for fifteen minutes. If that is unacceptable, the answer is a
shorter `exp`, and the cost of a shorter `exp` is more traffic to the
authorisation server — which is the trade-off, stated rather than hidden behind
a blacklist that would cost a query per request instead.

Users, roles and scopes are still absent. A `Principal` is an owner id. Anything
richer needs a claim this service does not read and an authorisation model
[ADR 3](0003-ownership-is-a-query-parameter.md) deliberately does not have.

Two identity adapters now exist where the code claimed one was enough to swap.
The dev adapter stays, because a template that needs an identity provider before
it runs at all is a template nobody tries.
