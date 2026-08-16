from dataclasses import dataclass, fields

from appcore.domain.errors import DomainError, DomainErrorCode


@dataclass(frozen=True, slots=True, repr=False)
class ValueObject:
    """Base for immutable, self-validating values compared by content.

    Subclasses override ``_validate`` rather than ``__post_init__``. That is
    not a style preference: ``@dataclass(slots=True)`` builds a *replacement*
    class object, which leaves the zero-argument ``super()`` closure pointing
    at the discarded original, so ``super().__post_init__()`` in a slotted
    subclass raises ``TypeError`` at construction time. A hook the base calls
    removes the trap instead of documenting it.
    """

    def __post_init__(self) -> None:
        if type(self) is ValueObject:
            raise DomainError(DomainErrorCode.BASE_CLASS_INSTANTIATION)
        if not fields(self):
            raise DomainError(
                DomainErrorCode.EMPTY_VALUE_OBJECT,
                {"class_name": type(self).__name__},
            )
        self._validate()

    def _validate(self) -> None:
        """Override to enforce invariants. Raise ``DomainError`` when violated."""
        return None

    def __repr__(self) -> str:
        items = fields(self)
        if len(items) == 1:
            body = repr(getattr(self, items[0].name))
        else:
            body = ", ".join(f"{f.name}={getattr(self, f.name)!r}" for f in items)
        return f"{type(self).__name__}({body})"
