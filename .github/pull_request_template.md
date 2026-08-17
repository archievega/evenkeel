## What was wrong

<!-- The problem, not the patch. If it is a behaviour change, what did the old
behaviour cost? -->

## What changed

<!-- Anything a reviewer would not guess from the diff. Deviations from an ADR
belong here, named. -->

## How you know

<!-- Delete the lines that do not apply. Do not tick one you have not done — an
unchecked box is information; a wrongly ticked one is the thing this repository
spends most of its effort catching. -->

- [ ] `make check` passes — exit code read, not the tail of the output
- [ ] A test fails without this change, and I watched it fail
- [ ] Numbers in the diff come from a committed command, not from a run I did once
- [ ] `make schema` run, if a route or response model moved
- [ ] `docs/SECURITY_CONTROLS.md` updated, if this adds or changes a control
- [ ] An ADR, if this turns on a decision somebody could reasonably make differently

## What this does not do

<!-- Optional, and the section reviewers read first. Known gaps, deliberate
omissions, the case you decided not to handle. -->
