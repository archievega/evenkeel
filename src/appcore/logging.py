import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "secret",
        "secret_key",
        "api_key",
        "private_key",
        "client_secret",
        "cookie",
        "set_cookie",
    }
)


def _is_sensitive(key: object) -> bool:
    normalized = "".join(ch for ch in str(key).casefold() if ch.isalnum())
    return (
        normalized in {k.replace("_", "") for k in SENSITIVE_KEYS}
        or normalized.endswith("token")
        or normalized.endswith("secret")
        or normalized.endswith("apikey")
        or normalized.endswith("password")
    )


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***REDACTED***" if _is_sensitive(key) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    return value


def redaction_processor(
    _logger: logging.Logger, _name: str, event_dict: EventDict
) -> EventDict:
    """Strip credentials from every log record.

    Redaction belongs in the pipeline, not at call sites: a leak needs only one
    call site that forgot, and log aggregation makes that permanent and
    searchable.
    """
    redacted: EventDict = redact_sensitive(dict(event_dict))
    return redacted


def setup_logging(
    *,
    level: str = "INFO",
    json_logs: bool = True,
    pretty_console: bool = False,
    include_timestamp: bool = True,
) -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        redaction_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if include_timestamp:
        shared.insert(0, structlog.processors.TimeStamper(fmt="iso", utc=True))

    renderer: Processor = (
        structlog.dev.ConsoleRenderer()
        if pretty_console or not json_logs
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        # The stdlib factory, not PrintLogger: uvicorn, SQLAlchemy and every
        # library log through `logging`, and routing structlog elsewhere gives
        # two output formats in one stream that no log parser can split.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=level.upper(), force=True
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
