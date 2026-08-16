import logging
import re
import sys
from collections.abc import Mapping, Set
from typing import Any, TextIO

import structlog
from structlog.types import EventDict, Processor

REDACTED = "***REDACTED***"

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


# `key=value` and `key: value` inside an already-formatted message. Structured
# redaction cannot reach those: by the time a library has rendered its line, the
# secret is a substring.
#
# Best-effort, and the limit is worth stating. SQLAlchemy's `echo` prints bound
# parameters positionally — `('s3cret',)` — with no key to match on, and nothing
# can classify that. `database.echo` is therefore refused outside a local run by
# the boot guard rather than pretended away here.
_INLINE_SECRET = re.compile(
    r"(?i)\b([a-z_-]*(?:password|token|secret|api[_-]?key|authorization))"
    r"(\s*[=:]\s*)"
    # The scheme is kept and the credential after it replaced. Matching the
    # first token alone redacted the word "Bearer" and published what followed,
    # which is a redaction that reads as working and is not.
    r"((?:Bearer|Basic|Token|Digest)\s+)?"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
)


def redact_message(message: str) -> str:
    return _INLINE_SECRET.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}{REDACTED}", message
    )


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_sensitive(key) else redact_sensitive(item)
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

    This is a denylist, and that is correct *here* only because the keys are
    ones we wrote by hand. For a payload nobody on this side authored, use
    `allowlisted` below instead.
    """
    redacted: EventDict = redact_sensitive(dict(event_dict))
    # A record from a library arrives already rendered, so its secret is inside
    # the message rather than in a field the walk above can see.
    event = redacted.get("event")
    if isinstance(event, str):
        redacted["event"] = redact_message(event)
    return redacted


def allowlisted(
    payload: Mapping[str, Any] | None,
    *,
    allow: Set[str],
    max_items: int = 20,
) -> dict[str, Any]:
    """Keep only the named keys; replace every other value with a marker.

    Use this for any structure this codebase did not author -- a provider
    response, a webhook body, a validation report from a library, anything whose
    shape can change without a commit here.

    The polarity is the entire point. A denylist has to predict what a secret
    will be called, so the day a provider adds `session_token` to its error
    payload, it is logged. An allowlist fails the other way: an unanticipated
    field is redacted rather than published, and the worst outcome is a missing
    diagnostic instead of a credential in the log aggregator.

    Keys are preserved so the shape of what was rejected is still visible; only
    the values disappear.
    """
    if not payload:
        return {}
    result: dict[str, Any] = {}
    for index, (key, value) in enumerate(payload.items()):
        if index >= max_items:
            result["..."] = f"{len(payload) - max_items} more keys"
            break
        result[str(key)] = value if key in allow else REDACTED
    return result


def setup_logging(
    *,
    level: str = "INFO",
    json_logs: bool = True,
    pretty_console: bool = False,
    include_timestamp: bool = True,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog and the stdlib root logger.

    `stream` defaults to stdout, which is right for a server whose output is
    collected by a container runtime. It is not right for every process: the
    MCP transport speaks JSON-RPC over stdout, and a log line written there is
    handed to the client as a malformed protocol message. That was not a
    hypothetical — the first end-to-end run of `evenkeel-mcp` printed a pydantic
    validation error at the client because "database engine disposed" arrived
    where a response was expected.
    """
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
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        # The stdlib factory, not PrintLogger: uvicorn, SQLAlchemy and every
        # library log through `logging`, and routing structlog elsewhere gives
        # two output formats in one stream that no log parser can split.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # Every record, including the ones this code did not write.
    #
    # `basicConfig` alone formatted structlog's output and left uvicorn's,
    # SQLAlchemy's and every library's untouched: a second format in the same
    # stream, and — the part that mattered — records that never met the
    # redaction processor. `foreign_pre_chain` runs it over them too.
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
