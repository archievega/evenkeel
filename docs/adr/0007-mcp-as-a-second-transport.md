# 7. MCP as a second transport, over the same use cases

Status: accepted

## Context

"Ports and adapters" is the easiest architecture to claim and one of the hardest
to verify. A codebase can have `domain/`, `application/` and `infrastructure/`
directories, a container, and a page of documentation about dependency
direction, and still be a web framework with extra folders — because with one
transport, nothing ever tests whether the layering is real. Every shortcut that
couples a use case to HTTP stays invisible: a status code returned from an
interactor, an owner read out of a request object three frames down, validation
that lives in a pydantic model and nowhere else.

The check is to add a second way in and see what breaks.

MCP is a good choice for that check for reasons beyond fashion. It has no status
codes, so anything that leaked one is exposed. It has no headers, so an identity
that was quietly travelling in one has nowhere to hide. And its caller is a
language model, which makes the authorization question sharper than any HTTP
client does.

## Decision

`presentation/mcp/` exposes six tools — open, deposit, withdraw, read, list,
ledger — each resolving the *same* interactor the HTTP router resolves, from the
same container.

**Nothing below `presentation` changed to add it.** No new port, no new method
on an existing one, no argument added to a command, no branch in a use case.
That is the entire claim, and `git show` is the evidence.

Everything that makes a movement safe is inherited rather than reimplemented:
the idempotency key, the per-wallet lock, the optimistic version check, the rate
limit, the outbound risk assessment, the owner scoping in SQL. A tool is roughly
twenty lines of translation.

### The owner is configuration, not a tool argument

`APP__MCP__OWNER_ID`, bound when the server is constructed. The model cannot see
it or set it, and `evenkeel-mcp` refuses to start without it rather than picking
a default.

A `deposit(owner_id, wallet_id, amount)` tool would be a cross-tenant IDOR with
a natural-language interface. The model reads untrusted text for a living —
support tickets, web pages, file contents — so "the model was persuaded to pass
a different id" is a normal Tuesday, not an exotic attack. The parameter that
does not exist cannot be manipulated.

The test asserting no tool has an owner-shaped parameter is
`test_no_tool_lets_the_caller_choose_an_owner`, and it inspects the published
schemas rather than the source.

### Errors travel as codes

`ApplicationError` and `DomainError` are translated once, in this layer, into a
tool error carrying `CODE: human sentence`. The HTTP layer does the same job
into RFC 9457. Neither translation lives in the use case, which is why adding
this transport did not require touching one.

`details` are deliberately not forwarded. On the HTTP side they are a documented
shape; here they would be `DomainError.context`, which is unfiltered and
sometimes carries the value that was rejected. A new surface should not inherit
a known gap.

### Tools are annotated with what they do

Reads are `read_only_hint`, movements are `destructive_hint`. Clients use these
to decide what needs human confirmation, so a mislabelled money movement is a
missing confirmation dialog rather than a documentation error.

### One request scope per tool call

The same boundary an HTTP request gets: a session, a transaction, and
interactors resolved against them. Sharing a scope across tool calls would share
a transaction across unrelated operations, so a failed call would roll back a
successful one.

## Consequences

**Three defects surfaced, all of them older than this transport.** None was in
the MCP code; all were things one transport had been hiding.

1. `Money` accepted any magnitude, including values `NUMERIC(20, 2)` cannot
   store. Over HTTP the amount happened to be constrained at the pydantic edge;
   through a tool it reached the database, where the refusal arrives as a driver
   error at commit and surfaces as a 500 for what is a bad request. Now bounded
   in the domain, so every transport gets the same answer before anything is
   written.
2. `Money` with `NaN` raised `TypeError` rather than a `DomainError` — the same
   story, and the reason a model sending `"NaN"` is a realistic input.
3. The test fakes returned insertion order where the SQL adapters return
   `ORDER BY id DESC`, and used `uuid4` where production uses UUIDv7 — so the
   fakes disagreed with the database about the order of every paginated read,
   and nothing noticed until a second transport asserted on it.

**And one in the new code, found by running it rather than by testing it.** The
first end-to-end run over real stdio printed a pydantic validation error at the
*client*: `setup_logging` writes to stdout, which in this process is the
protocol. `setup_logging` grew a `stream` parameter. Worth stating plainly —
the in-process tests all passed while this was broken, because they never spoke
the protocol.

**The transport is opt-in.** `mcp` is an extra, `presentation/mcp` is imported
by nothing else, and a deployment that does not want a model near its money
simply does not run `evenkeel-mcp`.

**What this does not include.** No authentication beyond the configured owner —
the server trusts the process that spawned it, which is the local-subprocess
model MCP clients use today. Exposing it over HTTP would need the identity story
that `DevIdentityProvider` is still standing in for, and that is the JWT/JWKS
slice, not this one.
