from evenkeel.application.ports import MetricsPort


class NoopMetrics(MetricsPort):
    """The default metrics adapter.

    Instrumentation calls stay in the code unconditionally; turning telemetry
    off swaps this in rather than sprinkling ``if metrics_enabled`` through
    every use case. Same reason the Prometheus adapter is an opt-in slice: the
    core must run with no monitoring stack attached.
    """

    def request_started(self) -> None:
        return None

    def request_finished(
        self,
        *,
        method: str,
        handler: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        return None

    def request_failed(
        self,
        *,
        method: str,
        handler: str,
        exception_type: str,
        duration_seconds: float,
    ) -> None:
        return None

    def observe_interactor(
        self, *, name: str, outcome: str, duration_seconds: float
    ) -> None:
        return None

    def observe_external_call(
        self, *, service: str, operation: str, outcome: str, duration_seconds: float
    ) -> None:
        return None

    def observe_lock(self, *, name: str, outcome: str, wait_seconds: float) -> None:
        return None

    def observe_rate_limit(self, *, policy: str, decision: str) -> None:
        return None
