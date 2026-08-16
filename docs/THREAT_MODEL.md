# Threat model

What this service protects, from whom, and — the half most threat models skip —
what it deliberately does not.

The controls themselves are in [SECURITY_CONTROLS.md](SECURITY_CONTROLS.md), one
row each: the claim, where it is enforced, and the test that proves it. This
document is the reasoning that produced those rows. They are cross-checked
against each other by `tests/unit/test_security_receipts.py`, so a control whose
test is renamed away fails the build rather than becoming a sentence nobody can
verify.

## Assets

| Asset | Why it is worth attacking | Loss looks like |
| --- | --- | --- |
| Wallet balances | It is money | A balance that moved without an authorised movement |
| The ledger | It is the audit trail | An entry that disagrees with the balance, or a missing one |
| Owner scoping | It separates tenants | One owner reading or moving another's |
| Credentials in transit and at rest | Reuse everywhere | A token in a log, a DSN in a response |
| The published contract | Clients build against it | A silent breaking change, or a document that lies |

Notably absent: personal data. The wallet carries an owner id and nothing else —
no name, no email, no address. That is a deliberate property of the example
domain, and it is why this document has no GDPR section.

## Trust boundaries

```
        untrusted                        │ trusted
                                         │
   HTTP client ──── bearer token ────────┤  presentation/http
   MCP client ───── stdio, no auth ──────┤  presentation/mcp
                                         │       │
                                         │  application  (no I/O of its own)
                                         │       │
   risk provider ◄── outbound HTTPS ─────┤  infrastructure/adapters
   Postgres ◄────── credentialed ────────┤
   Redis ◄───────── credentialed ────────┤
```

Four boundaries, and they are not equally strong.

**HTTP is the hostile one.** Everything arriving is attacker-controlled: the
body, the headers, the path, the token, the idempotency key, the cursor.

**MCP is hostile in a different way.** The transport is a local subprocess and
the client is trusted by construction, but the *model driving it* reads
untrusted text for a living — support tickets, web pages, file contents. Its
tool arguments are therefore attacker-influenced even though the transport is
not. This is why the owner is bound from configuration and is not a tool
parameter: the argument that does not exist cannot be manipulated
([ADR 7](adr/0007-mcp-as-a-second-transport.md)).

**The risk provider is a peer, not a dependency to be trusted.** It can be slow,
absent, or answer with something this code cannot parse. An unparseable answer
is never read as approval ([ADR 6](adr/0006-outbound-calls.md)).

**The database and Redis are inside the boundary** and hold the credentials that
say so. The application never composes SQL from input — every statement is
parameterised through SQLAlchemy Core — so the boundary is not load-bearing
against injection; it is load-bearing against a stolen credential, which is a
deployment concern this template can only refuse to make worse.

## Threats, and what answers them

STRIDE, minus the categories that have nothing to say about a wallet ledger.

### Spoofing — acting as another owner

The whole authorization model is one rule: **ownership is a query parameter,
never a check afterwards**. A read that forgets it returns nothing rather than
somebody else's row, because the filter is in the SQL
([ADR 3](adr/0003-ownership-is-a-query-parameter.md), CWE-639).

A wallet belonging to someone else is reported **absent, not forbidden**, so the
status code cannot be used to enumerate which ids exist (CWE-204).

Authentication is declared on the router rather than per endpoint, so a new
endpoint is protected by default and an unauthenticated one has to be written on
purpose (CWE-306).

Tokens are verified against the issuer's published key set, with the signing
algorithm taken from configuration and never from the token — a verifier that
trusts the header accepts `alg: none`, and accepts HMAC signed with the public
RSA key as the secret (CWE-347). Issuer and audience are both required, so a
valid token minted by the same issuer for a sibling service is refused here
([ADR 9](adr/0009-a-resource-server-not-an-auth-server.md)).

**The known weakness**: the bundled `DevIdentityProvider` treats the bearer
token as the owner id. Anyone can be anyone. It is a placeholder for local runs,
and the boot guard refuses to start with it — or with a JWT configuration
missing its issuer, audience or key set — outside one (CWE-1188).

**Not defended**: revocation before expiry. A stolen token works until `exp`.
The alternative is a blacklist consulted on every request, which trades a query
per request forever against a window a shorter `exp` already bounds.

### Tampering — money moving without an authorised movement

Three independent guards, because each covers what the others cannot
([ADR 4](adr/0004-three-guards-against-double-spend.md)):

| Guard | Covers | Misses |
| --- | --- | --- |
| Idempotency key, claimed before the work | A client retry | Two genuinely different requests |
| Per-wallet distributed lock | Interleaved read-modify-write | A writer that bypasses it |
| Optimistic version predicate | Everything, last | Nothing — it is the backstop |

Plus a `CHECK (balance >= 0)` in the database and the same invariant in the
entity constructor, which is also the read path
([ADR 2](adr/0002-persistence-via-core-not-orm-mapped-entities.md)).

The idempotency key is claimed *before* the work and confirmed after, so a store
failure in the window after the commit cannot produce a 503 for money that
already moved ([ADR 8](adr/0008-idempotency-is-claimed-before-the-work.md),
CWE-837). Keys are namespaced per owner, so one tenant's `k-42` cannot collide
with another's — or report on it (CWE-668).

Money is `Decimal` end to end and crosses every boundary as a string, so no
float rounding happens in transit and cross-currency arithmetic raises rather
than converting (CWE-681).

### Repudiation — "I never authorised that"

The ledger is append-only and every entry records `balance_after`, so the
balance can be reconstructed from the entries rather than trusted as a total.
Every response carries a correlation id, in the body and in a header, which is
the key to the server-side log line.

**Not defended**: there is no signed audit log and no tamper-evidence. Somebody
with database write access can edit history undetectably. That is a real gap for
a real financial system and out of scope for a template.

### Information disclosure

The rule is that an error explains the rule and never the input.

* A validation failure names the field and the constraint, never the rejected
  value — pydantic's raw errors carry it, so they are allowlisted (CWE-209).
* A domain refusal is allowlisted the same way, after shipping with the rejected
  currency echoed back (CWE-209).
* An unhandled exception returns a correlation id, never a stack trace or a DSN.
  `debug` is never passed to FastAPI, because Starlette's debug page renders
  before the handler is consulted (CWE-209).
* The readiness probe reports a failure class, not the driver's message, since
  that message contains the DSN and probe output is scraped and alerted on.
* Log redaction runs in the pipeline rather than at call sites — a leak needs
  only one call site that forgot — and covers records this codebase did not
  write, plus credentials already rendered into a message (CWE-532).
* Interactive docs and `/metrics` are off outside a local run (CWE-200).
* A correlation id supplied by the caller is replaced unless it is printable and
  bounded. It is echoed into a response header, bound into every log line and
  forwarded to providers, so taken verbatim it let an unauthenticated caller
  write their own log lines (CWE-93).

**The limit, stated rather than papered over**: SQLAlchemy's `echo` prints bound
parameters positionally, `('s3cret',)`, with no key to classify. No redactor can
do anything with that, so the boot guard refuses the flag outside a local run
instead of promising a filter that cannot work.

### Denial of service

Rate limiting is **per owner, not per IP** — an IP is shared by a corporate NAT
and rented by anyone who wants another one (CWE-770). Outbound calls are bounded
by a per-attempt timeout and an overall budget, with concurrency capped so a slow
provider cannot accumulate in-flight callers (CWE-1088, CWE-770). Outbound
response bodies are read to a cap, so a broken upstream cannot spend this
process's memory (CWE-400). Metric labels are a closed set, because unbounded
labels are the standard way to take a monitoring system down (CWE-770).

**Not defended**: a determined attacker. The rate limiter is a fairness
mechanism. There is no bot management, no WAF, no adaptive shedding, and the
template assumes something in front of it does that job.

### Elevation of privilege

There are no roles. Every authenticated caller has exactly one privilege: acting
on their own wallets. There is no admin surface, no impersonation, and no
endpoint that takes an owner id as input — the closest thing to privilege
escalation this API can express is an ownership filter that was forgotten, which
is the first threat above.

The container runs as an unprivileged user and ships without a package installer
(CWE-250).

## Supply chain

Dependencies are pinned by lockfile and audited (`pip-audit`), the image is
scanned (trivy, fixed-only HIGH and CRITICAL), and secrets are scanned across
full history rather than the tip — a credential committed and then removed is
still a credential (CWE-532). The published documentation loads one external
script, pinned by version with an integrity hash that CI recomputes against the
file the CDN actually serves.

Outbound proxy environment variables are ignored unless explicitly enabled, and
outbound redirects are never followed: both silently reroute traffic to a host
the configuration never named, which is a supply-chain problem rather than a
convenience (CWE-918). The redirect half matters most on the JWKS fetch — the
answer to that request is the set of keys that decide who every caller is.

## What this template does not do

Listed because a threat model that only describes wins is marketing.

* **No token issuing.** This is a resource server: it verifies, and there is no
  login endpoint, password hashing, refresh rotation or session store. The
  authorisation server is somebody else's, which is the only arrangement in
  which an access/refresh pair means anything.
* **No authorization beyond ownership.** No roles, no delegation, no scopes.
* **No secret management.** Secrets come from the environment. There is no
  vault integration, no rotation, no envelope encryption.
* **No tamper-evident audit.** Database write access is total.
* **No PII handling**, because the example domain has none. A real system needs
  retention, export and erasure that nothing here demonstrates.
* **No multi-tenant isolation beyond the query filter.** One database, one
  schema, `WHERE owner_id = :owner`. Row-level security and per-tenant
  encryption are deliberate absences.
* **No image signing or provenance attestation.** Cosign and SLSA are a separate
  project, not a template feature.
* **No DoS defence worth the name.** See above.

## Reviewing this document

It is wrong the moment the code changes without it. Two things keep that honest:
the controls matrix is cross-checked against the tests by
`tests/unit/test_security_receipts.py`, and every CWE named here appears in that
matrix. Neither catches a threat nobody thought of — for that, the only
mechanism is somebody reading it again.
