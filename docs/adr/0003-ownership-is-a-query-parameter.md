# 3. Ownership is a query parameter, not a check

Status: accepted

## Context

The common shape for per-user authorization is: load the resource by id, then
compare its owner to the caller and raise if they differ.

```python
resource = await repository.read(resource_id)
if not resource.is_owner(caller_id):
    raise Forbidden()
```

It works everywhere it is written. The vulnerability is the place someone
forgot.

The audit of the two source codebases found exactly that. One had ownership
checks in eight of ten use cases — the two missing ones were `delete_transaction`
and `update_transaction`, the only two that move money. Any user could delete or
rewrite another user's transaction and change their account balance. The handler
dutifully passed `profile_id` into the command, so the code read as if it were
enforced; the interactor used that id for the *category* and never for the
transaction.

Read endpoints are worse, because "it only returns data" feels harmless.

## Decision

Every repository read takes the owner it is performed on behalf of, and filters
in SQL:

```python
async def read(self, wallet_id: WalletId, owner_id: OwnerId, *, for_update: bool = False) -> Wallet | None
```

An unscoped read is not something you can express. There is no ownership check
to forget, because there is no unscoped query to forget it on. Entries are
scoped through a join to their wallet's owner, so the rule does not weaken one
level down.

A row that exists but belongs to someone else is reported as **absent**, not
forbidden. Answering "it exists, but is not yours" is an existence oracle: an
attacker enumerates valid ids from status codes alone.

Fakes enforce the same scoping. A fake that ignores `owner_id` makes every
ownership test pass for the wrong reason.

The same principle applies at the transport layer: routers are mounted
default-deny with `dependencies=[Depends(current_principal)]`. Per-endpoint
authentication makes each new route opt in to being protected, and the route
that forgets looks exactly like one that is intentionally public — which is how
an unauthenticated password-reset endpoint ships.

## Consequences

`WALLET_FORBIDDEN` no longer exists as an error code; there is nothing to raise
it. Ownership violations surface as 404.

Repository signatures are wider, and a genuine admin or background-job read
needs a deliberately named method rather than the absence of an argument. That
is the intended cost: the privileged path is visible in review instead of being
the default everything else quietly inherits.
