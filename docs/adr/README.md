# Architecture decisions

Short records of choices that are expensive to reverse, and of the reasoning
behind them. Each one exists because the alternative was tried somewhere and
failed in a specific way.

| # | Decision |
| --- | --- |
| [1](0001-enforce-layers-in-ci.md) | Layer rules are enforced in CI, not documented |
| [2](0002-persistence-via-core-not-orm-mapped-entities.md) | Persist with SQLAlchemy Core and explicit mapping |
| [3](0003-ownership-is-a-query-parameter.md) | Ownership is a query parameter, not a check |
| [4](0004-three-guards-against-double-spend.md) | Three independent guards on every balance change |
| [5](0005-null-adapters-for-optional-dependencies.md) | Every optional dependency ships a null adapter |
| [6](0006-outbound-calls.md) | Outbound calls carry four guards, and unavailable is an outcome |
| [7](0007-mcp-as-a-second-transport.md) | MCP over the same use cases, with the owner bound out of the model's reach |
| [8](0008-idempotency-is-claimed-before-the-work.md) | The idempotency key is claimed before the work, not written after it |

## Writing a new one

Number it sequentially, state the context as a problem someone actually hit, and
record the consequences including the ones you dislike. A decision record that
lists only benefits is marketing, and the next person will not trust it when
they need it.
