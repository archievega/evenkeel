"""Static proof that each adapter still satisfies the port it is bound to.

The DI container binds an implementation to a `Protocol` at runtime, so nothing
type-checks the pairing: a drifted adapter passes lint, passes mypy, passes
every test that uses a fake, and raises `TypeError` on the first real call.

Add one line here whenever an adapter is bound to a port in `setup/ioc`.
Forgetting is not hypothetical, so it is also a test —
`tests/unit/test_adapter_conformance.py` fails when an implementation of an
abstract port is not named below.
"""

from typing import TYPE_CHECKING

from evenkeel.application.ports import (
    BulkheadPort,
    Clock,
    DistributedLockPort,
    IdempotencyStore,
    IdGenerator,
    LedgerRepository,
    MetricsPort,
    RateLimiterPort,
    WalletRepository,
)
from evenkeel.application.ports.identity import IdentityProvider
from evenkeel.application.ports.risk import RiskAssessmentPort
from evenkeel.infrastructure.adapters.dev_identity import DevIdentityProvider
from evenkeel.infrastructure.adapters.memory.bulkhead import InMemoryBulkhead
from evenkeel.infrastructure.adapters.memory.idempotency import InMemoryIdempotencyStore
from evenkeel.infrastructure.adapters.memory.locking import (
    InMemoryDistributedLock,
    InMemoryRateLimiter,
)
from evenkeel.infrastructure.adapters.noop.metrics import NoopMetrics
from evenkeel.infrastructure.adapters.noop.risk import AllowAllRiskAssessment
from evenkeel.infrastructure.adapters.prometheus.metrics import PrometheusMetrics
from evenkeel.infrastructure.adapters.redis.bulkhead import RedisBulkhead
from evenkeel.infrastructure.adapters.redis.idempotency import RedisIdempotencyStore
from evenkeel.infrastructure.adapters.redis.locking import (
    RedisDistributedLock,
    RedisRateLimiter,
)
from evenkeel.infrastructure.adapters.sqla.ledger_repository import SqlaLedgerRepository
from evenkeel.infrastructure.adapters.sqla.wallet_repository import SqlaWalletRepository
from evenkeel.infrastructure.adapters.system import SystemClock, Uuid7Generator

if TYPE_CHECKING:
    # Behind the `outbound` extra. Imported for typing only, so this module
    # still loads in an install that does not have aiohttp.
    from evenkeel.infrastructure.adapters.http.risk import HttpRiskAssessment

    # Behind the `jwt` extra, for the same reason.
    from evenkeel.infrastructure.adapters.jwt.identity import JwtIdentityProvider


def _assert_wallet_repository(adapter: SqlaWalletRepository) -> WalletRepository:
    return adapter


def _assert_ledger_repository(adapter: SqlaLedgerRepository) -> LedgerRepository:
    return adapter


def _assert_clock(adapter: SystemClock) -> Clock:
    return adapter


def _assert_id_generator(adapter: Uuid7Generator) -> IdGenerator:
    return adapter


def _assert_identity_provider(adapter: DevIdentityProvider) -> IdentityProvider:
    return adapter


def _assert_lock(adapter: InMemoryDistributedLock) -> DistributedLockPort:
    return adapter


def _assert_rate_limiter(adapter: InMemoryRateLimiter) -> RateLimiterPort:
    return adapter


def _assert_idempotency_store(adapter: InMemoryIdempotencyStore) -> IdempotencyStore:
    return adapter


def _assert_metrics(adapter: NoopMetrics) -> MetricsPort:
    return adapter


def _assert_redis_lock(adapter: RedisDistributedLock) -> DistributedLockPort:
    return adapter


def _assert_redis_rate_limiter(adapter: RedisRateLimiter) -> RateLimiterPort:
    return adapter


def _assert_redis_idempotency(adapter: RedisIdempotencyStore) -> IdempotencyStore:
    return adapter


def _assert_bulkhead(adapter: InMemoryBulkhead) -> BulkheadPort:
    return adapter


def _assert_redis_bulkhead(adapter: RedisBulkhead) -> BulkheadPort:
    return adapter


def _assert_prometheus_metrics(adapter: PrometheusMetrics) -> MetricsPort:
    return adapter


def _assert_allow_all_risk(adapter: AllowAllRiskAssessment) -> RiskAssessmentPort:
    return adapter


def _assert_http_risk(adapter: "HttpRiskAssessment") -> RiskAssessmentPort:
    return adapter


def _assert_jwt_identity(adapter: "JwtIdentityProvider") -> IdentityProvider:
    return adapter
