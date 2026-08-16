from datetime import UTC, datetime
from uuid import UUID

import uuid_utils


class SystemClock:
    """Always timezone-aware UTC.

    Naive datetimes compare and subtract against aware ones by raising
    ``TypeError`` at runtime, and the mix only shows up once a second source of
    timestamps appears. One aware source removes the class of bug.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)


class Uuid7Generator:
    """UUIDv7: random like v4, but time-ordered.

    Random primary keys scatter inserts across the B-tree, so every insert
    dirties a different page. v7 keeps inserts at the right edge of the index
    and makes ``ORDER BY id`` a usable creation order, which is what the cursor
    pagination relies on.
    """

    def generate(self) -> UUID:
        return UUID(str(uuid_utils.uuid7()))
