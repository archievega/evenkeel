# 2. Persist with SQLAlchemy Core and explicit mapping

Status: accepted

## Context

The usual way to keep a domain model free of persistence concerns in SQLAlchemy
is imperative mapping: define plain classes, then map them to tables. Both
source codebases did this, and it works — until it doesn't, in two specific
ways.

**Validation stops running.** SQLAlchemy reconstructs loaded rows through
`__new__`, so `__init__` and `__post_init__` never execute on a read. Every
invariant enforced in the constructor is enforced on creation only. One of the
audited codebases had 27 imperative mappings and no `reconstructor`: an entity
whose constructor rejects a negative balance loaded a negative balance from the
database without complaint, and only exploded later, far from the cause.

**Writes happen where you did not write them.** With a mutated instance in the
session, a flush at an unrelated `await` point persists it. Reasoning about
"when does this change hit the database" means reasoning about autoflush.

Value objects make it worse: mapping a `Money` composed of a `Decimal` and a
`CurrencyCode` requires composite types, custom coercion, or flattening the
value object back out into the mapping.

## Decision

Domain entities are not mapped. Tables are declared in
`infrastructure/sqla/tables.py` and repositories translate rows to entities
explicitly, with `select()`, `insert()` and `update()`.

## Consequences

Validation runs on every read, because `_to_entity` constructs the value objects
through their real constructors. A malformed row fails loudly at the boundary
instead of propagating.

Writes are visible: a row changes where a repository method says it does. The
optimistic-concurrency update is a plain `UPDATE ... WHERE version = :expected`
returning a row count, rather than a `version_id_col` whose behaviour has to be
inferred.

It also sidesteps a trap the audit found in production code: `select(...)
.with_for_update()` through the ORM returns the *identity-map* instance, not the
locked row's current values, unless `populate_existing=True` is also passed. The
lock is taken and the attributes are stale — code that reviews as correct and
grants a bonus twice. With Core there is no identity map to be stale.

The cost is boilerplate: two translation functions per aggregate, and no lazy
loading or relationship traversal. For a template whose point is that the domain
stays honest, that trade is worth making — and the boilerplate is the kind a
scaffolder can generate.
