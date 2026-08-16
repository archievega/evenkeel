# 4. Three independent guards on every balance change

Status: accepted

## Context

A balance change is read-modify-write. Two requests that read the same balance
and both write produce a lost update: two ledger entries, one balance movement,
money invented or destroyed. This is not a rare interleaving — it is what
happens when a user double-taps a button.

The audit found this unguarded in one source codebase: plain `SELECT`, no row
lock, no version column, no advisory lock, `READ COMMITTED`, on every path that
moves money. A goal balance could be withdrawn N times concurrently because the
"sufficient funds" check was check-then-act.

Each available mechanism has a hole:

- **A distributed lock** serialises writers, and is unavailable exactly when
  Redis is. It also does nothing about a writer that bypasses it — a migration,
  a maintenance script, a future service.
- **An optimistic version check** always holds, and only detects the conflict
  after the work is done, turning contention into user-visible errors.
- **An idempotency key** stops a client retry, and says nothing about two
  genuinely different concurrent requests.

## Decision

All three, stacked, in `WalletMovementService` — the single place a balance
changes:

1. **Idempotency key** (optional, from `Idempotency-Key`). A repeat returns the
   original result with `replayed: true`. Reusing a key with a different payload
   is a 409 rather than a confirmation of an operation that never ran.
2. **Distributed lock** on `wallet:{id}`, so the read-decide-write window is not
   interleaved. Failure to acquire is a value (`lock.acquired`), not an
   exception, and surfaces as a retryable 409.
3. **Optimistic version**, as `UPDATE ... WHERE version = :expected`. Zero rows
   updated means a concurrent writer won; the transaction rolls back.

The database also carries `CHECK (balance_amount >= 0)`. An invariant enforced
only in the application is one that a migration or a psql session can violate.

## Consequences

Three mechanisms for one property is more than most code needs, and it is
deliberate: the in-memory lock adapter is correct for a single process and wrong
for two replicas, so the version check is what keeps the guarantee true when
someone scales the deployment before swapping the adapter.

Each guard is covered by a test that fails without it — the lost-update test
forces a version mismatch, the contention test uses a lock that never grants,
and the retry test asserts one ledger entry for two identical requests.

Deposit and withdraw share this path entirely; they differ only in which domain
method runs.
