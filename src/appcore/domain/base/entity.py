from typing import Any

from appcore.domain.base.identifier import Identifier
from appcore.domain.errors import DomainError, DomainErrorCode


class Entity[IdT: Identifier]:
    """Base for objects with a lifecycle, compared by identity rather than state.

    Identity is write-once: rebinding ``id_`` after construction is a bug that
    silently corrupts dictionaries, sets and the ORM identity map, so it raises
    instead of being allowed.
    """

    def __init__(self, *, id_: IdT) -> None:
        if type(self) is Entity:
            raise DomainError(DomainErrorCode.BASE_CLASS_INSTANTIATION)
        self.id_ = id_

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "id_" and getattr(self, "id_", None) is not None:
            raise DomainError(
                DomainErrorCode.IDENTITY_IS_IMMUTABLE,
                {"class_name": type(self).__name__},
            )
        super().__setattr__(name, value)

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return False
        return self.id_ == other.id_

    def __hash__(self) -> int:
        return hash((type(self), self.id_))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(id_={self.id_!r})"
