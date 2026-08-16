import base64
import binascii
from uuid import UUID

from appcore.application.errors import ApplicationErrorCode, ValidationError


def encode_cursor(last_id: UUID) -> str:
    """Opaque cursor over a time-ordered UUIDv7 primary key.

    Base64 signals "do not parse this" to clients. It is not a security
    boundary -- anyone can decode it -- so a cursor must never be the only
    thing standing between a caller and someone else's rows; the ownership
    filter in the query is what does that.
    """
    return base64.urlsafe_b64encode(last_id.bytes).decode()


def decode_cursor(cursor: str | None) -> UUID | None:
    if cursor is None:
        return None
    try:
        return UUID(bytes=base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, binascii.Error) as exc:
        raise ValidationError(
            ApplicationErrorCode.VALIDATION_FAILED,
            details={"cursor": "malformed cursor"},
        ) from exc
