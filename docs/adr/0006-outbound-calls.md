# 6. Calling someone else's service

Status: accepted

## Context

Every port in this codebase until now hid infrastructure the process owns: its
database, its Redis, its clock. Those fail in one way — they are up or they are
not — and the adapter's job is mostly translation.

A service operated by someone else fails in ways that have no analogue there. It
can be slow rather than down, which is the case that hurts most and the one no
health check notices. It can answer correctly and late. It can be up while the
network to it is not. It can be reachable and return a body that no longer
parses, because a field was renamed on a Tuesday.

The audit that started this template found the usual shape in production code:
`requests.post(url, json=payload)` with no timeout, inside a request handler,
inside a transaction. That call has no upper bound on how long it holds the
worker, and the fault it produces looks like a database problem.

## Decision

One outbound dependency exists — risk assessment on the money path — and it is
built as the reference for any that follow.

### The port hides HTTP entirely

`RiskAssessmentPort` deals in `RiskCheck` and `RiskDecision`. No status codes,
no headers, no retry counts. Two implementations: `AllowAllRiskAssessment`,
which is the default and is why `docker compose up` needs no vendor account, and
`HttpRiskAssessment`.

### Unavailable is an outcome, not an exception

`RiskOutcome` has three values: `ALLOWED`, `REFUSED`, `UNAVAILABLE`. Timeouts,
refused connections, 500s, malformed bodies and a full bulkhead all map to
`UNAVAILABLE`. Nothing in the adapter raises.

The distinction between `REFUSED` and `UNAVAILABLE` is load-bearing. They mean
opposite things — one is the provider working, the other is the provider
missing — and an adapter that reports a timeout as a refusal turns every network
incident into a fraud spike on the dashboards downstream.

### Four guards, each covering what the others cannot

In the order they apply:

1. **Bulkhead.** Concurrent occupancy is capped and refusal is immediate rather
   than queued — `wait_timeout_ms` is 0, because a caller who waits for a slot
   behind a caller who is timing out has simply moved the queue one layer up.
2. **Per-attempt timeout and an overall budget.** `budget_ms` is the number to
   quote as the worst case; without it the worst case is
   `max_attempts × timeout + backoff`, which nobody chose.
3. **Retries, only where they are safe**, with full jitter, honouring
   `Retry-After`. A 400 and a malformed 200 are not retried: they will be the
   same next time, and retrying doubles the load on a provider that is working
   to fix a bug that is ours.
4. **A response size cap**, read incrementally, so a broken or hostile upstream
   cannot spend this process's memory.

### The call happens before the lock and before the transaction

Holding a per-wallet lock across a call to someone else's service is how one
slow dependency becomes an outage on endpoints that never touch it. The cost is
that a retried request is assessed twice, which is why the client's idempotency
key is passed through to the provider.

### Fail closed by default

When the check cannot run, the movement is refused with a 503. An unassessed
movement is permanent; a 503 is not. `risk.fail_open` exists because this is a
decision a deployment gets to make, not a default to bury in an adapter — but it
has to be made deliberately.

### Refusal is a 403

Note the deliberate difference from a wallet owned by someone else, which is a
404 (ADR 0003). That 404 exists so the status cannot be used to discover which
ids are real. A risk refusal leaks nothing — the caller owns the wallet and
chose the amount — so it is answered plainly. The provider's stated reason stays
server-side: it names the rule that fired, and telling a caller which rule
refused them turns every refusal into tuning feedback for whoever is probing.

## Alternatives considered

**A circuit breaker.** Prototyped and not shipped. On this dependency the
bulkhead already provides what the breaker would: a refusal costs nothing
because excess callers are turned away rather than queued, and in-flight calls
are bounded. What the breaker added on measurement was around 55ms per refusal
against a fully dead provider, and a large loss of throughput against a
degraded-but-usable one, in exchange for the most intricate state machine in the
slice. It becomes worth revisiting for a dependency where calling a failing
instance is itself harmful — one that charges per request, or whose recovery is
prevented by the load — which this one is not.

## Consequences

Measured, not assumed — the full run is in `tools/load/README.md`.

**The bulkhead is worth roughly 20x on the cost of a refusal.** Provider slow,
200 requests per second: a shed write is refused in under a millisecond at the
median, against 449ms with no bulkhead, where every caller pays the provider's
latency before being told no.

**A guard is only worth what it is measured to be worth**, which is why the
prototype above is not in the codebase.

**A 503 does not say which guard fired, and that is correct but expensive.**
`bulkhead_full`, `timeout` and `budget_exhausted` are indistinguishable from
outside. Reading a load run without server-side metrics is guesswork; the
Prometheus adapter exists because two runs were interpreted wrongly before it
did.

**The layer contract costs a translation step.** `infrastructure` sits below
`setup`, so the adapter cannot read `RiskConfig`; the composition root turns
configuration into `TransportPolicy` and `SessionPolicy`. Slightly more code in
`providers/core.py`, and in exchange the adapter can be constructed in a test
with three explicit policies and no environment at all.
