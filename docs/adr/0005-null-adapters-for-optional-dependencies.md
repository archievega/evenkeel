# 5. Every optional dependency ships a null adapter

Status: accepted

## Context

Templates tend toward one of two failures. The minimal one runs immediately and
has nothing to teach: no metrics, no locking, no idempotency, so adding them
later means restructuring. The batteries-included one demonstrates everything
and requires Redis, Prometheus, a message broker and three API keys before it
serves a request — so the first experience of it is a stack trace.

The tempting middle ground is conditional instrumentation:

```python
if settings.metrics_enabled:
    metrics.observe(...)
```

which spreads a deployment concern through every use case, and guarantees that
the branch nobody runs locally is the branch that breaks in production.

## Decision

Every optional dependency is a port with at least two adapters, one of which
does nothing or does it in memory:

| Port | Default | Production |
| --- | --- | --- |
| `MetricsPort` | `NoopMetrics` | Prometheus |
| `DistributedLockPort` | `InMemoryDistributedLock` | Redis |
| `RateLimiterPort` | `InMemoryRateLimiter` | Redis |
| `IdempotencyStore` | `InMemoryIdempotencyStore` | Redis |
| `IdentityProvider` | `DevIdentityProvider` | JWT/JWKS |

Instrumentation calls are unconditional. Turning telemetry off swaps the
adapter; it does not add a branch.

Selection is one provider method reading config — `RedisConfig.enabled` is just
"is the URL set". No URL, no Redis.

## Consequences

`docker compose up` yields a working API with a database and nothing else, and
every code path that will run in production runs locally too, against a
different adapter.

Two obligations come with this:

**Null adapters must honour the contract.** `tests/contracts/` is one suite per
port that every implementation must pass — the in-memory and the Redis lock are
parameters to the same tests. Without it the two drift, and a use case that
passes in CI deadlocks in production. The Redis parameters skip unless
`TEST_REDIS_URL` is set, so the suite runs everywhere and gets stronger in CI.

**The default must be honest about being a default.** The in-memory lock is
correct for one process and wrong for two, which is why the aggregate also
carries a version check (see ADR 4). `DevIdentityProvider` treats the credential
as the owner id, which is why the production boot check refuses to start while
it is wired in. A null adapter that quietly pretends to be the real thing is
worse than no adapter at all.
