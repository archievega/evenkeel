# 1. Layer rules are enforced in CI, not documented

Status: accepted

## Context

Clean-architecture projects state their dependency rules in a README or a
contributing guide, and the rules hold until the first deadline. The failure is
not dramatic: someone imports a SQLAlchemy type into a use case because it is
five minutes faster, review does not catch it because the diff looks reasonable,
and a year later the domain cannot be tested without a database.

Both codebases this template was extracted from documented their layering
carefully. Both had violations: presentation modules importing ORM tables and
opening database sessions directly in route handlers, application modules
importing the composition root, and — in one — domain services importing
application ports, reversing the dependency arrow entirely.

Every one of those was written by someone who knew the rule.

## Decision

The layer rules live in `.importlinter` and run as a build step
(`make arch`, and a required CI job). Three contracts:

- **Layers.** `entrypoints → setup → presentation → infrastructure →
  application → domain`, imports only downward.
- **Domain purity.** `appcore.domain` may not import SQLAlchemy, FastAPI,
  Pydantic, Redis, structlog, dishka or anything else outside the standard
  library.
- **Application purity.** `appcore.application` may import ports; it may not
  import drivers, frameworks, or any sibling layer.

`appcore.logging` sits outside the layer stack deliberately. It is a leaf with
no dependencies, and forcing it into the order would either push logging beneath
the domain or duplicate it per layer.

## Consequences

The rules are now falsifiable. This was not theoretical: the first run of
`lint-imports` against this template failed, because `presentation` imported
`setup` for a logger and for the settings type. Both were fixed by moving the
logger to a leaf module and passing route prefixes as plain strings.

A violation is now a red build with the offending import printed, roughly two
seconds after it is introduced, rather than an architectural discussion a year
later.

The cost is real but small: a legitimate new cross-layer dependency requires
editing `.importlinter`, which is exactly the point — the edit makes the
decision visible in review.
