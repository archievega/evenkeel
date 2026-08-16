from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from appcore.application.errors import ApplicationError, ApplicationErrorCode
from appcore.domain.errors import DomainError, DomainErrorCode
from appcore.logging import allowlisted, get_logger

log = get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"

# What a client may see from a validation failure: where it happened, what is
# wrong, and which rule rejected it. Never the value that was rejected.
VALIDATION_ERROR_FIELDS = frozenset({"loc", "msg", "type"})

# A violated domain invariant is not automatically a client mistake, so the
# transport status is decided here rather than being carried by the domain.
# Anything unlisted falls back to 422.
DOMAIN_STATUS_OVERRIDES: dict[DomainErrorCode, int] = {
    DomainErrorCode.WALLET_INSUFFICIENT_FUNDS: 409,
    DomainErrorCode.WALLET_CLOSED: 409,
    DomainErrorCode.WALLET_NOT_EMPTY: 409,
}


def problem(
    *,
    status: int,
    code: str,
    title: str,
    request: Request,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """RFC 9457 Problem Details.

    A documented media type rather than a bespoke error envelope: clients get
    one shape across every endpoint, and ``code`` stays machine-readable while
    ``title`` stays human-readable.
    """
    body: dict[str, Any] = {
        "type": f"about:blank#{code.lower()}",
        "title": title,
        "status": status,
        "code": code,
        "instance": request.url.path,
    }
    correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
    if correlation_id:
        body["correlation_id"] = correlation_id
    if details:
        body["details"] = details
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_CONTENT_TYPE)


def setup_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApplicationError)
    async def handle_application_error(
        request: Request, exc: ApplicationError
    ) -> JSONResponse:
        log.warning(
            "application_error",
            code=str(exc.code),
            status_code=exc.status_code,
            path=request.url.path,
        )
        return problem(
            status=exc.status_code,
            code=str(exc.code),
            title=exc.message,
            request=request,
            details=exc.details,
        )

    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        status = DOMAIN_STATUS_OVERRIDES.get(exc.code, 422)
        log.warning(
            "domain_error", code=str(exc.code), status_code=status, path=request.url.path
        )
        return problem(
            status=status,
            code=str(exc.code),
            title=exc.code.value.replace("_", " ").capitalize(),
            request=request,
            details=exc.context,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic authors this structure, not us, so it is filtered by allowlist:
        # its raw errors also carry `input` -- the rejected value itself -- which
        # leaks whatever the client sent (a card number, a password typed into
        # the wrong field) into logs and error bodies, and breaks JSON encoding
        # on types like Decimal. New keys in a future pydantic release are
        # redacted by default rather than published.
        safe_errors = [
            allowlisted(dict(error), allow=VALIDATION_ERROR_FIELDS)
            for error in exc.errors()
        ]
        return problem(
            status=422,
            code=ApplicationErrorCode.VALIDATION_FAILED.value,
            title="Request failed validation",
            request=request,
            details={"errors": safe_errors},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The internal message never reaches the client: stack traces and driver
        # errors disclose schema and topology. The correlation id in the response
        # is what ties the user's report to this log line.
        log.error(
            "unhandled_exception",
            error_type=type(exc).__name__,
            path=request.url.path,
            exc_info=True,
        )
        return problem(
            status=500,
            code=ApplicationErrorCode.INTERNAL_ERROR.value,
            title="Internal server error",
            request=request,
        )
