# AGENTS.md

Rules for an agent editing this repository. Written for machines and for people
in a hurry: tables, not paragraphs.

This codebase's whole thesis is a rule set that an agent violates by default —
do not import a driver in `application`, scope every read by owner, register
each adapter binding, pass the bare constraint name in a migration. Most of the
rules below are enforced by a command. Run `make check` before claiming done.

## Commands

| Command | Checks | A failure means |
| --- | --- | --- |
| `make check` | everything CI runs | see the per-gate rows below |
| `make lint` | ruff format + ruff check | formatting or a lint rule |
| `make arch` | import-linter, 3 contracts | **you crossed a layer** — see Layers |
| `make types` | mypy strict on `src` | a real type error, or an adapter drifting from its port |
| `make schema-check` | `openapi.json` matches the app | you changed the HTTP contract; run `make schema` and review the diff |
| `make test` | everything needing no service | — |
| `make test-integration` | tests against real Postgres | needs Docker |
| `make schema` | regenerates `openapi.json` | run after any change to a route or response model |
| `make demo` | re-records `docs/demo.gif` | needs a running stack |
| `make new-vertical NAME=x` | scaffolds a vertical across the layers | — |

`make` alone lists every target, grouped.

## Layers

Dependencies point one way. `import-linter` fails the build on a violation, so
this is not advice.

| Layer | May import | Never imports |
| --- | --- | --- |
| `domain` | stdlib only | everything else, including pydantic and sqlalchemy |
| `application` | `domain`, `application.ports` | `infrastructure`, `presentation`, `setup`, any driver (`sqlalchemy`, `redis`, `aiohttp`, `fastapi`, `dishka`) |
| `infrastructure` | `application`, `domain`, drivers | `presentation`, `setup` |
| `presentation` | `application`, `domain` | `infrastructure`, `setup` |
| `setup` | everything | — |
| `entrypoints` | `setup` | — |

`evenkeel.logging` is deliberately outside the stack: a leaf with no
dependencies, importable from anywhere.

**If a layer needs something from below it, that something becomes a port.**
`application/ports/` holds the Protocol or ABC; `infrastructure/adapters/` holds
the implementation; `setup/ioc/providers/` binds them.

## Where things go

| Task | Files, in order |
| --- | --- |
| Add an endpoint | `make new-vertical NAME=<area>` writes the first four, then the three edits it prints: provider, router mount, `make schema` |
| Add a use case | `application/interactors/<area>/<name>.py` (Command + Result + Interactor) → export in the area `__init__.py` → provider → `tests/unit/` |
| Add an external dependency | `application/ports/<name>.py` (port + DTOs) → `infrastructure/adapters/<tech>/` → null adapter → `conformance.py` → provider → `tests/contracts/` |
| Add a config knob | `setup/config.py` (a `BaseModel`, with the reason in a comment) → provider → `.env.example` → `compose.yml` if the stack needs it |
| Change the database schema | `domain/entities/` → `infrastructure/sqla/tables.py` → **stop and ask a human** before writing a migration |
| Add a transport | `presentation/<name>/` + an entrypoint. Nothing below `presentation` should change; if it must, the design is wrong |

## Rules with teeth

### Ownership is a query parameter

```python
# DO — the filter is in the SQL, so it cannot be forgotten
wallet = await self._wallets.read(wallet_id, owner_id)

# DON'T — a check anyone can delete, and a 403 that leaks which ids exist
wallet = await self._wallets.read(wallet_id)
if wallet.owner_id != owner_id:
    raise ForbiddenError(...)
```

A wallet belonging to someone else is **404, not 403** (ADR 3).

### Money is `Decimal`, and crosses boundaries as a string

```python
# DO
{"amount": "10.00", "currency": "EUR"}
Money(amount=Decimal("10.00"), currency=CurrencyCode("EUR"))

# DON'T — JSON numbers are doubles on the other side of most parsers
{"amount": 10.0}
```

### Transactions are explicit and closed on every branch

```python
# DO
await self._transaction_manager.commit()      # success
await self._transaction_manager.rollback()    # no-op, refusal, or error

# DON'T — a session handed on mid-transaction is the next request's problem
raise ConflictError(...)                      # with an open transaction
```

### Errors carry a code, not a status

```python
# DO — the transport decides what the code means
raise NotFoundError(ApplicationErrorCode.WALLET_NOT_FOUND, details={...})

# DON'T — this couples a use case to HTTP, and the MCP transport has no 404
raise HTTPException(status_code=404)
```

### Every adapter is named in `conformance.py`

```python
def _assert_thing(adapter: MyRedisThing) -> ThingPort:
    return adapter
```

dishka binds implementation to Protocol at runtime; nothing type-checks the
pair without this. `tests/unit/test_adapter_conformance.py` fails if you forget.

### Optional dependencies ship a null adapter and never break the boot

```python
# DO
try:
    from evenkeel.presentation.http.routers.docs import router
except ImportError:
    log.warning("scalar_unavailable", extra_required="docs")
```

An extra that can stop the application is not optional.

## Anti-patterns

Each of these cost real debugging time here. Do not re-introduce them.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `TypeError` from `super().__post_init__()` | `slots=True` rebuilds the class; the zero-arg `super()` cell points at the discarded original | base class calls a `_validate()` hook subclasses override |
| Adapter passes lint, mypy and tests, then `TypeError` in production | DI binds impl→Protocol at runtime and nothing checks the pair | add the entry to `conformance.py` |
| `presentation` importing `setup` for a logger or settings | convenience | use the leaf `evenkeel.logging`; pass config values as arguments |
| Alembic constraint named `ck_wallets_ck_wallets_...` | the naming convention prefixes a name that already carries the prefix | pass the **bare** constraint name |
| DSN parses wrong, or alembic crashes printing it | f-string DSN; `@` in a password redirects the host, `%` detonates in ConfigParser | `URL.create()` per component |
| Container boots, endpoint 500s on an optional feature | the extra is in `make sync --all-extras` but not in the Dockerfile | add `--extra <name>` to the Dockerfile too |
| A metric labelled `error` for an ordinary conflict | `outcome` initialised to `"error"` and only overwritten on success | label by the error code |
| A load run that measures the rate limiter | 30 movements/owner/minute by default | raise `RATE_LIMIT` for the run, do not work around it in the script |
| Logs corrupt an stdio protocol | `setup_logging` writes to stdout, which `evenkeel-mcp` uses for JSON-RPC | pass `stream=sys.stderr` |
| A comment quotes a number nobody can reproduce | it was measured once, by hand, and the code moved | regenerate it from a committed tool, or delete the number |

## Comments

The research is unkind to the obvious instinct here: in ablations, comments are
worth between nothing and slightly negative to a model, *except* the ones that
explain a decision — and misleading ones measurably degrade output, because a
model internalises them rather than ignoring them. Every line is also read on
every visit, out of a finite attention budget.

So:

| | |
| --- | --- |
| **Write** | the constraint that is not derivable from the code: an alternative that was tried and failed, an external contract, "do the obvious thing here and X breaks" |
| **Do not write** | what the code already says; the story of how the code got here; a measured number that no committed tool reproduces |
| **Move out** | the long argument → `docs/adr/`, with a one-line pointer in the file; a rule that spans files → this document **and a check**, because a comment in one file is invisible to somebody editing another |

Module docstrings stay near ten lines. If the reasoning needs more, it is an
ADR, and the file gets a pointer to it.

The last row is not theory. `conformance.py` carried its rule in a comment for
weeks and three adapters were added without it; the comment did nothing and a
test caught them the same afternoon it was written.

## Stop and ask a human

- **Any database schema change.** Never run `alembic revision --autogenerate`
  on your own. Describe the DDL you intend and wait.
- **Any change to an HTTP response shape or status code.** `openapi.json` is a
  published contract; `oasdiff` will fail the pull request, and that is the
  point.
- **Adding a dependency.** The policy is in `PLAN.md` §2: the core takes only
  what is load-bearing and widely adopted; convenience lives behind a port and
  is demonstrated with the most ordinary tool available. A template that
  preaches swappable infrastructure must not hard-wire a niche library into its
  own example.
- **Weakening a check to make something pass.** Lowering a coverage gate,
  adding a `.trivyignore`, broadening an `except`, or `# type: ignore` without a
  reason are all changes to the repository's argument, not to its code.

## Facts that are easy to get wrong

- Python 3.13, `uv` for everything. Never `pip install` into the venv.
- SQLAlchemy **Core**, not ORM-mapped entities (ADR 2). Entities are plain
  dataclasses; the mapping is explicit in the repository adapters.
- Ids are **UUIDv7**, which is why `ORDER BY id DESC` is time order and why the
  test fakes use `Uuid7Generator` rather than `uuid4`.
- `docs/demo.gif` is generated from `tools/demo/api.tape`. Do not hand-edit it.
- `openapi.json` is generated by `tools/dump_openapi.py`. Do not hand-edit it.
- Comments explain **why**, never what. A comment restating the line below it
  will be removed in review.
- No emoji in code or commit messages.

## Reading order for a new area

1. `docs/adr/` — the decisions and the reasoning, newest first
2. `README.md` — what the template claims
3. `docs/SECURITY_CONTROLS.md` — control → enforcement → proof, with CWE ids
4. `PLAN.md` — the working plan, open defects, and every trap already hit
   (untracked; ask the human if it is not there)
