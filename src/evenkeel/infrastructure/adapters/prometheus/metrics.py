"""The real `MetricsPort`, behind the `metrics` extra.

Written when a load run turned out to be unreadable without it: the bulkhead
shedding a request, a timeout, and an exhausted retry budget all reach the
client as the same 503. See tools/load/README.md.

Every label is a closed set — route templates and enum outcomes, never an id.
Each distinct value is a time series, and unbounded labels are the standard way
to take a Prometheus instance down.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from evenkeel.application.ports import MetricsPort

# Buckets chosen for this service rather than inherited from the library
# default, which starts at 5ms and is mostly wasted on an API where a fast
# response is already 10ms.
#
# The boundaries at 0.8 and 1.5 are the outbound per-attempt timeout and the
# overall budget, because that is where this service's slow requests actually
# pile up — just under one guard or the other.
#
# Putting them there is the whole point, and the first version did not. It went
# 0.5, 1.0, 2.5, so the entire tail landed in one bucket a second and a half
# wide. `histogram_quantile` interpolates linearly inside a bucket, so the panel
# reported a p99 above the slowest request k6 measured client-side on the same
# run — an estimate larger than anything that happened. A histogram is only as
# honest as its boundaries, and a wide bucket does not read as uncertainty, it
# reads as a number.
_REQUEST_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    0.8,
    1.0,
    1.5,
    2.0,
    3.0,
    5.0,
    10.0,
)
_OUTBOUND_BUCKETS = (0.01, 0.05, 0.1, 0.2, 0.4, 0.8, 1.5, 3.0, 10.0)


class PrometheusMetrics(MetricsPort):
    """One registry per process.

    Under `workers > 1` each worker keeps its own registry and answers `/metrics`
    for itself, so a scrape hits whichever worker the socket hands it. That is a
    real limitation and the reason `prometheus_client` ships a multiprocess mode;
    it is not wired up here because the deployment this template assumes is one
    process per container, which is the arrangement that makes the problem
    disappear rather than the one that hides it.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        self._in_flight = Gauge(
            "evenkeel_http_requests_in_flight",
            "Requests currently being served.",
            registry=self.registry,
        )
        self._requests = Counter(
            "evenkeel_http_requests_total",
            "Requests completed, by outcome.",
            ["method", "handler", "status"],
            registry=self.registry,
        )
        self._request_duration = Histogram(
            "evenkeel_http_request_duration_seconds",
            "Time to serve a request.",
            ["method", "handler"],
            buckets=_REQUEST_BUCKETS,
            registry=self.registry,
        )
        self._request_failures = Counter(
            "evenkeel_http_request_failures_total",
            "Requests that ended in an unhandled exception.",
            ["method", "handler", "exception"],
            registry=self.registry,
        )
        self._interactors = Counter(
            "evenkeel_interactor_total",
            "Use case executions, by outcome.",
            ["name", "outcome"],
            registry=self.registry,
        )
        self._interactor_duration = Histogram(
            "evenkeel_interactor_duration_seconds",
            "Time spent inside a use case.",
            ["name"],
            buckets=_REQUEST_BUCKETS,
            registry=self.registry,
        )
        # The one this file was written for. `outcome` separates `success`,
        # `timeout`, `server_error`, `bulkhead_full` and `budget_exhausted` —
        # the distinction the client cannot see, and the only way to know which
        # guard fired.
        self._external_calls = Counter(
            "evenkeel_external_call_total",
            "Calls to a dependency, by outcome.",
            ["service", "operation", "outcome"],
            registry=self.registry,
        )
        self._external_duration = Histogram(
            "evenkeel_external_call_duration_seconds",
            "Time spent waiting on a dependency, including retries.",
            ["service", "operation"],
            buckets=_OUTBOUND_BUCKETS,
            registry=self.registry,
        )
        self._locks = Counter(
            "evenkeel_lock_total",
            "Lock acquisitions, by outcome.",
            ["name", "outcome"],
            registry=self.registry,
        )
        self._lock_wait = Histogram(
            "evenkeel_lock_wait_seconds",
            "Time spent waiting for a lock.",
            ["name"],
            buckets=_OUTBOUND_BUCKETS,
            registry=self.registry,
        )
        self._rate_limits = Counter(
            "evenkeel_rate_limit_total",
            "Limiter decisions.",
            ["policy", "decision"],
            registry=self.registry,
        )

    def request_started(self) -> None:
        self._in_flight.inc()

    def request_finished(
        self, *, method: str, handler: str, status_code: int, duration_seconds: float
    ) -> None:
        self._in_flight.dec()
        self._requests.labels(method, handler, str(status_code)).inc()
        self._request_duration.labels(method, handler).observe(duration_seconds)

    def request_failed(
        self, *, method: str, handler: str, exception_type: str, duration_seconds: float
    ) -> None:
        self._in_flight.dec()
        self._request_failures.labels(method, handler, exception_type).inc()
        self._request_duration.labels(method, handler).observe(duration_seconds)

    def observe_interactor(
        self, *, name: str, outcome: str, duration_seconds: float
    ) -> None:
        self._interactors.labels(name, outcome).inc()
        self._interactor_duration.labels(name).observe(duration_seconds)

    def observe_external_call(
        self, *, service: str, operation: str, outcome: str, duration_seconds: float
    ) -> None:
        self._external_calls.labels(service, operation, outcome).inc()
        self._external_duration.labels(service, operation).observe(duration_seconds)

    def observe_lock(self, *, name: str, outcome: str, wait_seconds: float) -> None:
        self._locks.labels(name, outcome).inc()
        self._lock_wait.labels(name).observe(wait_seconds)

    def observe_rate_limit(self, *, policy: str, decision: str) -> None:
        self._rate_limits.labels(policy, decision).inc()
