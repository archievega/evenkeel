from dataclasses import dataclass
from uuid import UUID

from evenkeel.domain.base.value_object import ValueObject


@dataclass(frozen=True, slots=True, repr=False)
class Identifier(ValueObject):
    """Base for typed identifiers.

    A bare ``UUID`` is assignable to any parameter that takes a ``UUID``, so
    passing a wallet id where an owner id belongs type-checks and fails at
    runtime. Distinct subclasses make that a static error instead.
    """

    value: UUID
