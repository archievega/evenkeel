from enum import StrEnum
from typing import Any, ClassVar


class ApplicationErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    WALLET_NOT_FOUND = "WALLET_NOT_FOUND"
    WALLET_VERSION_CONFLICT = "WALLET_VERSION_CONFLICT"
    WALLET_BUSY = "WALLET_BUSY"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    RATE_LIMITED = "RATE_LIMITED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


APPLICATION_ERROR_MESSAGES: dict[ApplicationErrorCode, str] = {
    ApplicationErrorCode.VALIDATION_FAILED: "Request failed validation",
    ApplicationErrorCode.WALLET_NOT_FOUND: "Wallet not found",
    ApplicationErrorCode.WALLET_VERSION_CONFLICT: "Wallet was modified concurrently",
    ApplicationErrorCode.WALLET_BUSY: "Wallet is being modified, retry shortly",
    ApplicationErrorCode.IDEMPOTENCY_KEY_REUSED: (
        "Idempotency key was already used with a different payload"
    ),
    ApplicationErrorCode.RATE_LIMITED: "Too many requests",
    ApplicationErrorCode.DEPENDENCY_UNAVAILABLE: (
        "A required dependency is unavailable, retry shortly"
    ),
    ApplicationErrorCode.INTERNAL_ERROR: "Internal server error",
}


class ApplicationError(Exception):
    """Base for use-case failures.

    The status code lives on the class so the transport layer needs a single
    generic handler rather than one handler per error type, and so a new error
    cannot be introduced without deciding what it means to a client.
    """

    default_status_code: ClassVar[int] = 422

    def __init__(
        self,
        code: ApplicationErrorCode,
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.message = APPLICATION_ERROR_MESSAGES.get(code, str(code))
        self.details = details or {}
        self.status_code = (
            self.default_status_code if status_code is None else status_code
        )
        super().__init__(self.message)


class ValidationError(ApplicationError):
    default_status_code = 422


class NotFoundError(ApplicationError):
    default_status_code = 404


class ForbiddenError(ApplicationError):
    default_status_code = 403


class ConflictError(ApplicationError):
    default_status_code = 409


class RateLimitedError(ApplicationError):
    default_status_code = 429


class DependencyUnavailableError(ApplicationError):
    """An infrastructure dependency could not be reached.

    Distinct from `InternalError`: this one is transient and retryable, which is
    what 503 tells a client and a load balancer. Adapters raise it instead of
    letting a driver exception escape, so application code can react to "Redis
    is down" without importing `redis`.
    """

    default_status_code = 503


class InternalError(ApplicationError):
    default_status_code = 500
