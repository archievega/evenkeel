# Security Policy

## Reporting a vulnerability

Report privately through GitHub's **Security → Report a vulnerability** form on
this repository. Please do not open a public issue for an unfixed vulnerability.

Include the affected version or commit, reproduction steps, and the impact you
believe it has. Expect an acknowledgement within 3 working days and an
assessment within 10.

## Supported versions

| Version | Supported |
| --- | --- |
| `main` | yes |
| tagged releases | latest minor only |

## What this template guarantees, and what it does not

Security properties that are enforced in code and covered by tests:

- **Ownership is a query parameter.** Every repository read takes the owner it
  acts on behalf of and filters in SQL, so an unscoped read cannot be written.
  A row belonging to someone else is reported as absent rather than forbidden,
  so status codes are not an existence oracle.
- **Routers are default-deny.** Authentication is declared on the router; a new
  endpoint is protected unless it explicitly opts out.
- **Secrets never reach structlog records.** Redaction runs as a processor in
  the pipeline rather than at call sites. Note the boundary: records emitted by
  libraries through stdlib `logging` — uvicorn, SQLAlchemy's `echo` — do not
  pass through it yet. See the open gap in `docs/SECURITY_CONTROLS.md`.
- **Errors do not leak internals.** Unhandled exceptions return a correlation id;
  the stack trace stays server-side. Validation errors echo the field and the
  reason, never the rejected value.
- **Production refuses to start insecurely.** Outside an explicitly `local`
  run, a default signing secret, a well-known database password, or the
  placeholder identity adapter aborts the boot instead of logging a warning
  that scrolls away. `environment` defaults to `production`, so an image
  deployed with no configuration gets the strict posture.

What it deliberately does **not** provide:

- The bundled `DevIdentityProvider` treats the credential as the owner id. It
  is a placeholder for local work, and the boot check now genuinely rejects it
  outside `local` — `app.identity_provider` is named in configuration precisely
  so the guard has something to refuse. Swap in a real identity adapter before
  exposing anything.
- No secret manager integration. Configuration comes from the environment;
  delivering secrets to that environment is the deployment's responsibility.
- No WAF, no bot management, no DDoS protection. The rate limiter is a
  fairness mechanism, not a defence against a determined attacker.
