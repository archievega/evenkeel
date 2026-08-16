# Security controls

Every control this template claims, where it is enforced, and the test that
proves it. A control with no proof is listed as one — that is the point of the
table, and writing it is what found the five gaps at the bottom.

CWE ids are the taxonomy the scanners already speak: `bandit` emits them, and
naming the weakness makes the intent legible to a reviewer who knows it.

## Authorization

| Control | Enforced in | Proven by | CWE |
| --- | --- | --- | --- |
| Every wallet read is filtered by owner in SQL, so an unscoped query cannot be written | [`ports/repositories.py`](../src/evenkeel/application/ports/repositories.py), [`adapters/sqla/wallet_repository.py`](../src/evenkeel/infrastructure/adapters/sqla/wallet_repository.py) | `test_another_owner_gets_nothing`, `test_listing_returns_only_the_owner_rows` | [639](https://cwe.mitre.org/data/definitions/639.html) |
| Ledger entries are scoped through a join to the owning wallet, so knowing an entry id is worthless | [`adapters/sqla/ledger_repository.py`](../src/evenkeel/infrastructure/adapters/sqla/ledger_repository.py) | `test_another_owner_reads_nothing`, `test_a_single_entry_is_scoped_too` | 639 |
| A row owned by someone else is reported absent, not forbidden — no existence oracle | [`interactors/wallets/read_wallets.py`](../src/evenkeel/application/interactors/wallets/read_wallets.py) | `test_another_owner_sees_the_wallet_as_absent` (404, not 403) | [204](https://cwe.mitre.org/data/definitions/204.html) |
| The write path is scoped identically to reads | [`services/wallet_movement.py`](../src/evenkeel/application/services/wallet_movement.py) | `test_another_owner_cannot_touch_the_wallet` | 639 |

## Authentication

| Control | Enforced in | Proven by | CWE |
| --- | --- | --- | --- |
| Authentication is declared on the router, so a new endpoint is protected unless it opts out | [`routers/v1/wallets.py`](../src/evenkeel/presentation/http/routers/v1/wallets.py) | `test_a_request_without_credentials_is_rejected` | [306](https://cwe.mitre.org/data/definitions/306.html) |
| The auth dependency performs no writes, so a read endpoint stays a read | [`http/dependencies.py`](../src/evenkeel/presentation/http/dependencies.py) | — *by construction; see gap 6* | — |

## Disclosure

| Control | Enforced in | Proven by | CWE |
| --- | --- | --- | --- |
| Credentials are stripped from every log record by a pipeline processor, not at call sites | [`logging.py`](../src/evenkeel/logging.py) | `test_a_credential_is_stripped`, `test_anything_ending_in_token_is_stripped`, `test_nested_structures_are_walked` | [532](https://cwe.mitre.org/data/definitions/532.html) |
| Payloads this codebase did not author are filtered by allowlist, so a new field is redacted by default | `logging.allowlisted`, [`http/errors.py`](../src/evenkeel/presentation/http/errors.py) | `test_everything_else_is_redacted`, `test_a_new_field_is_redacted_without_anyone_updating_a_list` | 532 |
| A rejected value never appears in the validation response | [`http/errors.py`](../src/evenkeel/presentation/http/errors.py) | `test_a_negative_amount_is_rejected_at_the_edge` | [209](https://cwe.mitre.org/data/definitions/209.html) |
| The readiness probe reports a failure class, never the DSN | [`routers/health.py`](../src/evenkeel/presentation/http/routers/health.py) | `test_readiness_does_not_leak_connection_details` | 209 |
| An unhandled exception returns a correlation id, never a stack trace | [`http/errors.py`](../src/evenkeel/presentation/http/errors.py) | `test_the_exception_message_never_reaches_the_client`, `test_the_response_carries_a_correlation_id_to_find_the_log` | 209 |
| Interactive docs are off outside debug | [`setup/app_factory.py`](../src/evenkeel/setup/app_factory.py) | `test_docs_are_absent_outside_debug` (and the inverse, so the test fails if docs break everywhere) | [200](https://cwe.mitre.org/data/definitions/200.html) |

## Integrity under concurrency

| Control | Enforced in | Proven by | CWE |
| --- | --- | --- | --- |
| A balance write fails if another writer advanced the row first | `wallet_repository.update` with a version predicate | `test_a_stale_version_does_not_overwrite`, `test_two_concurrent_sessions_cannot_both_commit` | [362](https://cwe.mitre.org/data/definitions/362.html) |
| The read-decide-write window is serialised per wallet | [`services/wallet_movement.py`](../src/evenkeel/application/services/wallet_movement.py) | `test_a_contended_wallet_reports_busy_rather_than_waiting_forever`, `test_the_write_path_locks_the_row` | 362 |
| `for_update=True` emits a real row lock | `wallet_repository.read` | `test_for_update_blocks_a_second_reader` (`FOR UPDATE NOWAIT` from a second transaction) | 362 |
| A retried request applies once | `services/wallet_movement.py`, `IdempotencyStore` | `test_retrying_with_the_same_key_does_not_move_money_twice`, `test_a_retried_deposit_is_applied_once` | [837](https://cwe.mitre.org/data/definitions/837.html) |
| One key with a different payload is refused rather than confirmed | same | `test_reusing_a_key_for_a_different_amount_is_rejected` | 837 |
| Work abandoned by an exception leaves nothing behind | DI session provider, `TransactionManager` | `test_a_write_abandoned_by_an_exception_does_not_survive` | — |
| The database refuses a negative balance independently of the application | migration `0001`, CHECK constraint | `test_the_check_constraint_rejects_a_negative_balance` | — |
| Money is `Decimal` end to end; cross-currency arithmetic raises | [`domain/value_objects/money.py`](../src/evenkeel/domain/value_objects/money.py) | `test_balance_survives_a_repeated_fractional_amount`, `test_mixing_currencies_raises_instead_of_adding` | [681](https://cwe.mitre.org/data/definitions/681.html) |

## Availability

| Control | Enforced in | Proven by | CWE |
| --- | --- | --- | --- |
| Per-owner rate limiting on money movement, refused before the wallet is read | [`interactors/wallets/move_money.py`](../src/evenkeel/application/interactors/wallets/move_money.py) | `test_a_denied_request_moves_no_money`, `test_the_limiter_runs_before_the_wallet_is_even_read` | [770](https://cwe.mitre.org/data/definitions/770.html) |
| Concurrent occupancy of a dependency is capped | [`ports/bulkhead.py`](../src/evenkeel/application/ports/bulkhead.py) | `test_concurrent_callers_never_exceed_the_limit` — *port only; no caller yet* | 770 |
| A dependency outage surfaces as a typed 503, not a driver traceback | [`adapters/redis/_errors.py`](../src/evenkeel/infrastructure/adapters/redis/_errors.py) | `test_a_connection_failure_becomes_a_typed_503`, `test_a_bug_in_the_adapter_keeps_its_own_identity` | [755](https://cwe.mitre.org/data/definitions/755.html) |
| Readiness fails when the database is unreachable, so traffic drains | [`routers/health.py`](../src/evenkeel/presentation/http/routers/health.py) | `test_readiness_reports_503_when_the_database_is_gone` | — |
| Liveness ignores dependencies, so a database blip does not restart healthy replicas | same | `test_liveness_ignores_dependencies` | — |

## Configuration and supply chain

| Control | Enforced in | Proven by | CWE |
| --- | --- | --- | --- |
| Production refuses to boot with a default secret or a well-known database password | [`setup/config.py`](../src/evenkeel/setup/config.py), [`entrypoints/web.py`](../src/evenkeel/entrypoints/web.py) | `test_the_default_secret_is_refused`, `test_the_default_settings_are_not_production_ready` | [1188](https://cwe.mitre.org/data/definitions/1188.html) |
| The runtime image runs as an unprivileged user and contains no package installer | [`Dockerfile`](../Dockerfile) | CI `build and scan image` (trivy, HIGH/CRITICAL, fixed-only) | [250](https://cwe.mitre.org/data/definitions/250.html) |
| Secrets are scanned across full history, not just the tip | CI `security` job | gitleaks over `fetch-depth: 0` | 532 |
| Dependencies are audited and statically analysed | CI `security` job | `pip-audit`, `bandit` | [1395](https://cwe.mitre.org/data/definitions/1395.html) |
| Layer boundaries cannot be crossed | [`.importlinter`](../.importlinter) | CI `lint-imports`, 3 contracts | — |
| An adapter cannot drift from the port it is bound to | [`adapters/conformance.py`](../src/evenkeel/infrastructure/adapters/conformance.py) | CI `mypy` | — |

## Known gaps

Gaps 1 to 5 were found by writing this table and closed in the same change; the
tests are now cited above. Writing the matrix was worth it for what it exposed,
not for what it confirmed.

**What closing them found.** Two of the controls were not merely untested, they
were broken:

- An unhandled exception produced a response with **no correlation id at all**,
  in neither the body nor the header, and the `unhandled_exception` log line had
  none either. Cause: Starlette installs the catch-all `Exception` handler on
  `ServerErrorMiddleware`, the outermost layer, so it runs *after* our
  middleware's `finally` has cleared the structlog context — and it writes its
  response through its own `send`, bypassing the wrapper that injects the
  header. The id is now carried on the ASGI scope, which belongs to the request
  and no layer can wipe, and the header is set on the problem document itself.
  This predates the pure-ASGI rewrite; the ordering is the same either way.
- `DenyingRateLimiter` had been sitting in `tests/fakes/system.py` used by
  nothing, so the interactor's rate-limit branch had never executed in a test.

One remains open:

6. "The auth dependency performs no writes" is true by construction and not
   asserted. Worth a test once a real identity adapter exists, since that is
   where the temptation to upsert a user on first sight appears.

## Deliberately not controls

The bundled `DevIdentityProvider` treats the credential as the owner id. It is a
placeholder, which is why gap 5's boot guard refuses production while it is
wired in. There is no secret-manager integration, no WAF, and no bot management;
the rate limiter is a fairness mechanism, not a defence against a determined
attacker. See [SECURITY.md](../SECURITY.md).
