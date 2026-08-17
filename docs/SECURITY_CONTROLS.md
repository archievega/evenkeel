# Security controls

Every control this template claims, where it is enforced, and the test that
proves it. A control with no proof is listed as one — that is the point of the
table, and writing it is what found the five gaps at the bottom.

CWE ids are the taxonomy the scanners already speak: `bandit` emits them, and
naming the weakness makes the intent legible to a reviewer who knows it.

## Authorization

The reasoning that produced these rows — assets, boundaries, and what is
deliberately not defended — is in [THREAT_MODEL.md](THREAT_MODEL.md).

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
| The signing algorithm comes from configuration; the token's own `alg` header is never consulted | [`adapters/jwt/identity.py`](../src/evenkeel/infrastructure/adapters/jwt/identity.py) | `test_an_algorithm_outside_the_configured_allowlist_is_refused`, `test_the_verifier_never_offers_an_algorithm_policy_did_not_name`, `test_a_key_published_for_encryption_cannot_verify_a_signature`, `test_a_token_signed_by_a_different_key_is_refused`, `test_a_token_without_a_key_id_is_refused_without_a_fetch` | [347](https://cwe.mitre.org/data/definitions/347.html) |
| Issuer, audience and expiry are all required, so a valid token minted for another service is refused here | [`adapters/jwt/identity.py`](../src/evenkeel/infrastructure/adapters/jwt/identity.py) | `test_a_token_that_fails_a_claim_is_refused`, `test_a_valid_token_becomes_a_principal`, `test_a_subject_that_is_not_a_canonical_uuid_is_refused`, `test_nothing_is_accepted_without_a_credential` | 306 |
| A refusal never says which check failed, so it cannot be used to probe whether a forged signature worked | [`adapters/jwt/identity.py`](../src/evenkeel/infrastructure/adapters/jwt/identity.py) | `test_the_refusal_never_says_which_check_failed` | [209](https://cwe.mitre.org/data/definitions/209.html) |
| The key set is cached and refreshed under one lock, and unknown key ids cannot force a fetch each | [`adapters/jwt/keys.py`](../src/evenkeel/infrastructure/adapters/jwt/keys.py) | `test_the_key_set_is_fetched_once_for_many_tokens`, `test_a_cold_burst_does_not_stampede_the_provider`, `test_unknown_key_ids_do_not_become_a_fetch_each`, `test_a_slow_refresh_does_not_stall_tokens_whose_key_is_in_memory` | [770](https://cwe.mitre.org/data/definitions/770.html) |
| A key set that is unreachable, unparseable or oversized is a typed 503, and keys already held outlive the outage | [`adapters/jwt/keys.py`](../src/evenkeel/infrastructure/adapters/jwt/keys.py) | `test_an_unreachable_key_set_is_a_dependency_failure`, `test_a_key_set_that_is_not_json_is_a_dependency_failure`, `test_a_key_set_entry_that_is_not_an_object_is_a_503_not_a_crash`, `test_an_outage_inside_the_refresh_floor_is_a_503_not_a_401`, `test_a_non_numeric_expiry_is_refused_rather_than_crashing`, `test_one_unusable_key_does_not_discard_the_rest`, `test_keys_already_held_survive_the_provider_going_down`, `test_a_key_set_split_across_segments_is_read_whole`, `test_a_claim_of_the_wrong_type_is_refused_rather_than_crashing` | [755](https://cwe.mitre.org/data/definitions/755.html) |
| Withdrawing a key takes effect: an empty key set revokes, a duplicate id cannot displace a live key, and the stale-key fallback has a ceiling | [`adapters/jwt/keys.py`](../src/evenkeel/infrastructure/adapters/jwt/keys.py) | `test_an_empty_key_set_revokes_rather_than_reading_as_an_outage`, `test_a_duplicate_key_id_cannot_displace_the_live_key`, `test_stale_keys_stop_being_an_answer_past_the_grace_ceiling`, `test_the_grace_ceiling_holds_inside_the_refresh_floor` | [613](https://cwe.mitre.org/data/definitions/613.html) |
| An outbound body is read whole, however many segments it arrives in, so a provider's answer is never silently truncated into a failure | [`adapters/http/transport.py`](../src/evenkeel/infrastructure/adapters/http/transport.py) | `test_a_response_larger_than_one_segment_is_read_whole`, `test_a_key_set_split_across_segments_is_read_whole` | [755](https://cwe.mitre.org/data/definitions/755.html) |
| The auth dependency performs no writes, so a read endpoint stays a read | [`http/dependencies.py`](../src/evenkeel/presentation/http/dependencies.py) | — *by construction; see gap 6* | — |

## Disclosure

| Control | Enforced in | Proven by | CWE |
| --- | --- | --- | --- |
| Credentials are stripped from every **structlog** record by a pipeline processor, not at call sites (library records via stdlib `logging` are not covered — gap 7) | [`logging.py`](../src/evenkeel/logging.py) | `test_a_credential_is_stripped`, `test_anything_ending_in_token_is_stripped`, `test_nested_structures_are_walked` | [532](https://cwe.mitre.org/data/definitions/532.html) |
| Payloads this codebase did not author are filtered by allowlist, so a new field is redacted by default | `logging.allowlisted`, [`http/errors.py`](../src/evenkeel/presentation/http/errors.py) | `test_everything_else_is_redacted`, `test_a_new_field_is_redacted_without_anyone_updating_a_list` | 532 |
| A rejected value never appears in the validation response | [`http/errors.py`](../src/evenkeel/presentation/http/errors.py) | `test_a_negative_amount_is_rejected_at_the_edge` | [209](https://cwe.mitre.org/data/definitions/209.html) |
| The readiness probe reports a failure class, never the DSN | [`routers/health.py`](../src/evenkeel/presentation/http/routers/health.py) | `test_readiness_does_not_leak_connection_details` | 209 |
| An unhandled exception returns a correlation id, never a stack trace — in every environment, because `debug` is no longer passed to FastAPI | [`http/errors.py`](../src/evenkeel/presentation/http/errors.py), [`setup/app_factory.py`](../src/evenkeel/setup/app_factory.py) | `test_the_exception_message_never_reaches_the_client`, `test_the_response_carries_a_correlation_id_to_find_the_log` | 209 |
| Interactive docs are off outside a local run | [`setup/app_factory.py`](../src/evenkeel/setup/app_factory.py) | `test_docs_are_absent_outside_local` (and the inverse, so the test fails if docs break everywhere) | [200](https://cwe.mitre.org/data/definitions/200.html) |

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
| Outside a `local` run, production refuses to boot with a default secret, a well-known database password, or the placeholder identity adapter | [`setup/config.py`](../src/evenkeel/setup/config.py), [`entrypoints/web.py`](../src/evenkeel/entrypoints/web.py) | `test_the_default_secret_is_refused`, `test_an_identity_provider_that_is_not_a_known_one_is_refused`, `test_the_placeholder_identity_provider_is_refused`, `test_the_default_settings_are_not_production_ready`, `test_a_token_verifier_missing_its_own_identity_is_refused`, `test_a_key_set_url_that_is_not_https_is_refused`, `test_a_key_set_url_that_cannot_work_is_refused`, `test_an_empty_algorithm_allowlist_is_refused`, `test_a_plaintext_risk_provider_is_refused_whatever_the_case` | [1188](https://cwe.mitre.org/data/definitions/1188.html) |
| The runtime image runs as an unprivileged user and contains no package installer | [`Dockerfile`](../Dockerfile) | CI `build and scan image` (trivy, HIGH/CRITICAL, fixed-only) | [250](https://cwe.mitre.org/data/definitions/250.html) |
| Secrets are scanned across full history, not just the tip | CI `security` job | gitleaks over `fetch-depth: 0` | 532 |
| Dependencies are audited and statically analysed | CI `security` job | `pip-audit`, `bandit` | [1395](https://cwe.mitre.org/data/definitions/1395.html) |
| Layer boundaries cannot be crossed | [`.importlinter`](../.importlinter) | CI `lint-imports`, 3 contracts | — |
| An outbound call cannot hang: every attempt is bounded, and the whole call including retries is bounded by a budget | [`http/transport.py`](../src/evenkeel/infrastructure/adapters/http/transport.py) | `test_a_timeout_is_a_failure_value_not_an_exception`, `test_the_budget_bounds_the_worst_case` | [1088](https://cwe.mitre.org/data/definitions/1088.html) |
| A slow or dead dependency is shed rather than accumulated: concurrent calls are capped and excess is refused immediately, not queued | [`http/transport.py`](../src/evenkeel/infrastructure/adapters/http/transport.py) | `test_a_full_bulkhead_refuses_without_calling_the_provider`, `tools/load/README.md` runs B–F | [770](https://cwe.mitre.org/data/definitions/770.html) |
| An outbound redirect is never followed, so a provider that can be made to answer `302` cannot reroute the call — or the key set that decides who every caller is | [`http/transport.py`](../src/evenkeel/infrastructure/adapters/http/transport.py), [`adapters/jwt/keys.py`](../src/evenkeel/infrastructure/adapters/jwt/keys.py) | `test_a_redirect_is_not_followed`, `test_the_key_set_is_not_fetched_through_a_redirect` | [918](https://cwe.mitre.org/data/definitions/918.html) |
| A correlation id from the caller is replaced unless it is printable and bounded — it reaches a response header, every log line and an outbound header | [`http/middleware.py`](../src/evenkeel/presentation/http/middleware.py) | `test_a_hostile_correlation_id_is_replaced`, `test_a_well_formed_correlation_id_is_kept`, `test_the_last_valid_correlation_id_wins` | [93](https://cwe.mitre.org/data/definitions/93.html) |
| A key set that dribbles cannot hold the refresh open, because the session's own ceiling bounds it | [`adapters/jwt/keys.py`](../src/evenkeel/infrastructure/adapters/jwt/keys.py) | `test_a_dribbling_key_set_cannot_hold_the_refresh_open` | 770 |
| An outbound response cannot exhaust memory: the body is read to a cap, not to completion — proven against an endless one, and after decompression rather than before | [`http/transport.py`](../src/evenkeel/infrastructure/adapters/http/transport.py) | `test_an_oversized_body_is_refused_rather_than_read`, `test_an_implausibly_large_key_set_is_refused`, `test_the_size_cap_applies_after_decompression`, `test_read_capped_stops_at_the_limit`, `test_the_key_set_cap_is_a_size_somebody_chose` | [400](https://cwe.mitre.org/data/definitions/400.html) |
| A provider response this code cannot parse is never read as approval | [`http/risk.py`](../src/evenkeel/infrastructure/adapters/http/risk.py) | `test_an_unrecognised_decision_is_not_permission` | [754](https://cwe.mitre.org/data/definitions/754.html) |
| The outbound destination comes from configuration and is never taken from a request; proxy environment variables are ignored unless asked for | [`setup/config.py`](../src/evenkeel/setup/config.py), [`http/risk.py`](../src/evenkeel/infrastructure/adapters/http/risk.py) | `trust_env=False` by default; `risk.base_url` is not reachable from any handler | [918](https://cwe.mitre.org/data/definitions/918.html) |
| A plaintext `http://` risk endpoint is refused in production, because the request carries an owner id, an amount and a bearer token | [`setup/config.py`](../src/evenkeel/setup/config.py) | `production_config_problems` | [319](https://cwe.mitre.org/data/definitions/319.html) |
| A risk refusal does not tell the caller which rule fired | [`interactors/wallets/move_money.py`](../src/evenkeel/application/interactors/wallets/move_money.py) | `test_a_refusal_does_not_say_which_rule_fired` | [209](https://cwe.mitre.org/data/definitions/209.html) |
| Metric labels are a closed set: route templates and enum outcomes, never ids | [`prometheus/metrics.py`](../src/evenkeel/infrastructure/adapters/prometheus/metrics.py) | `test_the_handler_label_is_a_route_template_not_a_path` | [770](https://cwe.mitre.org/data/definitions/770.html) |
| An APP-scoped client is closed at shutdown rather than left to the garbage collector | [`ioc/providers/core.py`](../src/evenkeel/setup/ioc/providers/core.py) | every Redis provider yields through `_redis_client`; the aiohttp session is owned by `open_http_risk_assessment` | [404](https://cwe.mitre.org/data/definitions/404.html) |
| A movement records its key before the work, and a store outage after the commit is swallowed rather than reported — the caller is never told a committed movement failed | [`services/wallet_movement.py`](../src/evenkeel/application/services/wallet_movement.py) | `TestIdempotencyStoreContract` (8 rules, both adapters), `test_two_in_flight_requests_with_one_key_apply_once`, `test_a_store_outage_after_the_commit_does_not_report_failure` | [837](https://cwe.mitre.org/data/definitions/837.html) |
| Idempotency keys are namespaced per owner, so one tenant's key cannot collide with — or report on — another's | [`services/wallet_movement.py`](../src/evenkeel/application/services/wallet_movement.py) | `test_one_owners_idempotency_key_does_not_collide_with_anothers`, `test_reusing_a_key_with_a_different_payload_is_still_refused` | [668](https://cwe.mitre.org/data/definitions/668.html) |
| Log redaction covers records this codebase did not write — uvicorn, SQLAlchemy, any library on `logging` — and credentials embedded in an already-rendered message | [`logging.py`](../src/evenkeel/logging.py) | `test_a_record_from_a_library_is_redacted_too`, `test_a_secret_inside_a_message_is_redacted` | [532](https://cwe.mitre.org/data/definitions/532.html) |
| `database.echo` is refused outside a local run, because SQLAlchemy prints bound parameters positionally and no redactor can classify them | [`setup/config.py`](../src/evenkeel/setup/config.py) | `test_the_boot_guard_refuses_sql_echo` | [532](https://cwe.mitre.org/data/definitions/532.html) |
| A domain refusal explains the rule without echoing the value that was rejected | [`http/errors.py`](../src/evenkeel/presentation/http/errors.py) | `test_a_domain_error_does_not_echo_the_rejected_value`, `test_a_domain_error_still_explains_the_rule` | [209](https://cwe.mitre.org/data/definitions/209.html) |
| The default lock adapter refuses instead of waiting at its own default, and reclaims a lease its holder never released | [`memory/locking.py`](../src/evenkeel/infrastructure/adapters/memory/locking.py) | `test_the_default_wait_refuses_instead_of_waiting`, `test_an_expired_lease_is_reclaimed` | [833](https://cwe.mitre.org/data/definitions/833.html) |
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

Closed by the outbound slice (2026-08-16):

- `MetricsPort.observe_external_call` had one no-op implementation and no
  caller: a metric that could not be seen. A load run made this concrete —
  `bulkhead_full`, `timeout` and `budget_exhausted` all reach the client as the
  same 503, and two runs were interpreted wrongly before the Prometheus adapter
  existed. See `tools/load/README.md`.
- Four Redis clients were constructed by DI providers that `return`ed rather
  than `yield`ed, so nothing closed them at shutdown.
- The movement rate limit was a literal in application code with no way to reach
  it from configuration. A limit that needs a release to change is not a
  control.

Open:

6. "The auth dependency performs no writes" is true by construction and not
   asserted. Worth a test once a real identity adapter exists, since that is
   where the temptation to upsert a user on first sight appears.

## Deliberately not controls

The bundled `DevIdentityProvider` treats the credential as the owner id. It is a
placeholder, which is why gap 5's boot guard refuses production while it is
wired in. There is no secret-manager integration, no WAF, and no bot management;
the rate limiter is a fairness mechanism, not a defence against a determined
attacker. See [SECURITY.md](../SECURITY.md).
