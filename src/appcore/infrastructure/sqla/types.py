from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class TzAwareDateTime(TypeDecorator[datetime]):
    """The single place where timezone correctness is enforced.

    Mixing naive and aware datetimes raises ``TypeError`` only when the two
    finally meet, which is usually in production and usually far from the code
    that introduced the naive one. Normalising in the column type makes it
    impossible to skip: a helper function you must remember to call is a helper
    function someone will forget.

    On the way in, naive values are assumed UTC rather than rejected, because
    the alternative is a 500 on data that is almost certainly already UTC. On
    the way out, values are always aware.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        result: datetime = value
        if result.tzinfo is None:
            return result.replace(tzinfo=UTC)
        return result.astimezone(UTC)
