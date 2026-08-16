from abc import ABC, abstractmethod


class MetricsPort(ABC):
    """Observability as a port, with a no-op default.

    Every label here is a bounded, low-cardinality category. Never pass user
    ids, wallet ids, emails or raw error strings: each distinct label value is
    a separate time series, and unbounded labels are the standard way to take
    down a Prometheus instance.
    """

    @abstractmethod
    def request_started(self) -> None: ...

    @abstractmethod
    def request_finished(
        self,
        *,
        method: str,
        handler: str,
        status_code: int,
        duration_seconds: float,
    ) -> None: ...

    @abstractmethod
    def request_failed(
        self,
        *,
        method: str,
        handler: str,
        exception_type: str,
        duration_seconds: float,
    ) -> None: ...

    @abstractmethod
    def observe_interactor(
        self,
        *,
        name: str,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        """Record a use case outcome: ``success``, ``rate_limited``, ``conflict``..."""

    @abstractmethod
    def observe_external_call(
        self,
        *,
        service: str,
        operation: str,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        """Record a call to a dependency. ``outcome``: ``success``/``timeout``/``http_5xx``."""

    @abstractmethod
    def observe_lock(
        self,
        *,
        name: str,
        outcome: str,
        wait_seconds: float,
    ) -> None:
        """Record a lock attempt. ``outcome``: ``acquired``/``timeout``/``error``."""

    @abstractmethod
    def observe_rate_limit(self, *, policy: str, decision: str) -> None:
        """Record a limiter decision. ``decision``: ``allowed``/``denied``."""
