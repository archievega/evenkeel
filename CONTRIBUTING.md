# Contributing

This is a template. The most useful thing you can do with it is take it and
change everything — no attribution, no upstream. If you want to change *this*
copy, read on.

## The short version

```bash
make sync          # dev environment
make check         # lint, layers, types, schema, tests — everything CI runs
```

`make check` is the whole gate. If it passes locally it passes in CI, and if it
fails, the table in [AGENTS.md](AGENTS.md#commands) says what each failure
means.

Read the exit code, not the output. `make check | tail -3` returns zero whatever
came before it, which is how a red commit reached `main` here once.

## What gets a change merged

**A behaviour change comes with a test that fails without it.** Not a test that
passes — one you have watched fail. Two of the tests in this repository were
written for a property they never checked, and both were found by breaking the
code on purpose and noticing the suite stayed green. So: break your fix, run the
test, watch it fail, put the fix back. If you cannot make it fail, the test is
not testing your change.

**A claim comes with a receipt.** Every security control in
[SECURITY_CONTROLS.md](docs/SECURITY_CONTROLS.md) names a test, and
`tests/unit/test_security_receipts.py` fails if a named test does not exist or
does not carry the CWE the row assigns it. Prose asserting a property nothing
verifies is the failure mode this repository is most careful about, because it
is the one that looks finished.

**A number comes from a command.** A comment quoting a measurement nobody can
reproduce is worse than no comment: it reads as evidence. Either regenerate it
from something committed — `make load`, `make dashboard-image` — or drop the
number and keep the shape of the argument.

**A layer violation is not negotiable.** `make arch` runs import-linter over
three contracts, and it is the one gate with no judgement call in it. If your
change needs `application` to import a driver, the change is wrong, not the
contract. [ADR 1](docs/adr/0001-enforce-layers-in-ci.md) says why it is a
command rather than a paragraph.

## Where things go

The table in [AGENTS.md](AGENTS.md#where-things-go) maps a kind of change to the
files it touches, and the answer is usually more files than you expect: a new
adapter also needs a line in `infrastructure/adapters/conformance.py`, a new
route also needs `make schema`, a new outbound dependency also needs a null
adapter ([ADR 5](docs/adr/0005-null-adapters-for-optional-dependencies.md)).

`make new-vertical NAME=x` scaffolds one across every layer and prints the three
things it cannot do for you.

## Decisions

If your change turns on a decision somebody could reasonably make differently,
write an ADR. [docs/adr/README.md](docs/adr/README.md) has the shape: state the
context as a problem someone actually hit, and record the consequences you
dislike along with the ones you want. A record that lists only benefits is
marketing, and the next person will not trust it when they need it.

Nine exist. [ADR 6](docs/adr/0006-outbound-calls.md) is the one to read first if
you want the tone — it ends by deleting a component the same ADR introduced,
with the measurements that killed it.

## Commits and pull requests

One purposeful change per commit, with a subject line that says what changed and
a body that says what was wrong. The history is linear; rebase rather than merge.

No emoji, in code or in commit messages.

## Reporting a vulnerability

Not through an issue. [SECURITY.md](SECURITY.md) has the process.

## Working with an agent

[AGENTS.md](AGENTS.md) is written for one, and is worth reading even if you are
not: it is the same rule set, without the paragraphs. The anti-pattern table in
it lists the mistakes this codebase has actually made, each with the fix, which
is a shorter route to the house style than reading the source.
