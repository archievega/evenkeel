# 8. The idempotency key is claimed before the work, not written after it

Status: accepted

Amends [ADR 4](0004-three-guards-against-double-spend.md), which introduced the
key but not the order.

## Context

The obvious shape, and the one this codebase had: do the work, commit, then
write the record.

```python
await self._transaction_manager.commit()
await self._remember(request, wallet, entry)     # <- the window
```

Between those two lines the money has moved and nothing remembers the key. If
the store is unreachable in that moment — a Redis failover, a network blip, a
timeout — the caller gets a 503 for a movement that succeeded, and their retry
finds an empty store and moves it again. The store being down is exactly the
condition under which clients retry hardest.

The window is small and the consequence is a double spend, which is the
combination that makes it worth fixing rather than documenting.

There was a second problem in the same code. The replay check ran twice, once
outside the per-wallet lock for the common retry and once inside it for the
genuine race, because neither placement alone was correct. That arrangement had
already produced one bug: the first attempt kept only the inner check and
answered 409 for a movement that had completed.

## Decision

Two phases. `IdempotencyStore` becomes `reserve` / `confirm` / `release`.

**Reserve** claims the key atomically, before anything happens, and reports
what was already there:

| Outcome | Meaning | Answer |
| --- | --- | --- |
| `RESERVED` | the key is yours | proceed |
| `IN_PROGRESS` | another caller holds it right now | 409 `MOVEMENT_IN_PROGRESS` |
| `COMPLETED` | it finished; the record is attached | replay it |
| `CONFLICT` | same key, different payload | 409 `IDEMPOTENCY_KEY_REUSED` |

**Confirm** attaches the result after the commit. **Release** gives the key back
when the work did not happen — insufficient funds, no such wallet, a lost
version race — so a refusal does not burn the key for the length of the TTL.

Atomicity is the point, so the Redis adapter does it in a Lua script rather than
`SET NX` followed by `GET`: between those two calls another caller can confirm,
expire or replace the entry, and the answer would then describe a state that no
longer exists.

The double-check disappears. Two concurrent requests with one key cannot both be
`RESERVED`, so the claim outside the lock is now the only one needed, and the
regression that arrangement caused cannot recur.

### A duplicate still in flight is refused, not answered

`IN_PROGRESS` returns 409 rather than waiting for the original. There is no
result to replay — the winner has not finished, and may yet fail — so any answer
would be invented. Stripe refuses a concurrent duplicate the same way, and the
client's move is the same as for any 409.

### A replay reports the balance it was written with

The record carries the ledger entry, and the entry carries `balance_after`. The
previous code returned the original entry beside the wallet's *current* balance,
so a wallet that moved on in between produced a single response that disagreed
with itself, against ADR 4's promise of "the original result".

## Consequences

**The remaining window is bounded and does not double-spend.** If `confirm`
fails after the commit, the reservation survives with its TTL: a retry inside
that window gets `IN_PROGRESS` — honest, and retryable — rather than moving the
money a second time. After the TTL a retry would apply again, which is the same
guarantee any idempotency key gives.

**One more round trip per movement.** Reserve, then confirm, where there used to
be a get and a put — the same two calls, differently placed. Nothing measurable
changed in the load runs.

**The store now holds state, not just results.** A reservation nobody confirms
or releases lingers until its TTL, so a crashed process leaves keys claimed for
up to 24 hours. Shortening the TTL trades that against how long a legitimate
retry is protected; there is no setting that avoids the trade.

**Both adapters are held to the same eight rules** by the contract suite, which
is what makes the in-memory store trustworthy in tests. The `SET ... EX 0` case
that started that suite is still in it.
