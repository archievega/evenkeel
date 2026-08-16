from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager

from dishka import AnyOf, Provider, Scope, from_context, provide
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from evenkeel.application.ports import (
    BulkheadPort,
    Clock,
    DistributedLockPort,
    IdempotencyStore,
    IdGenerator,
    MetricsPort,
    RateLimiterPort,
    TransactionManager,
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
from evenkeel.infrastructure.adapters.noop.risk import AllowAllRiskAssessment
from evenkeel.infrastructure.adapters.system import SystemClock, Uuid7Generator
from evenkeel.logging import get_logger
from evenkeel.setup.config import (
    HTTP_RISK,
    AppConfig,
    DatabaseConfig,
    MovementPolicyConfig,
    ObservabilityConfig,
    RedisConfig,
    RiskConfig,
    Settings,
)

log = get_logger(__name__)


class ConfigProvider(Provider):
    scope = Scope.APP

    settings = from_context(provides=Settings)

    @provide
    def app_config(self, settings: Settings) -> AppConfig:
        return settings.app

    @provide
    def database_config(self, settings: Settings) -> DatabaseConfig:
        return settings.database

    @provide
    def redis_config(self, settings: Settings) -> RedisConfig:
        return settings.redis

    @provide
    def observability_config(self, settings: Settings) -> ObservabilityConfig:
        return settings.observability

    @provide
    def risk_config(self, settings: Settings) -> RiskConfig:
        return settings.risk

    @provide
    def movement_policy_config(self, settings: Settings) -> MovementPolicyConfig:
        return settings.movements


class DatabaseProvider(Provider):
    scope = Scope.APP

    @provide
    async def engine(self, config: DatabaseConfig) -> AsyncIterable[AsyncEngine]:
        engine = create_async_engine(
            url=config.async_dsn.get_secret_value(),
            echo=config.echo,
            echo_pool=config.echo_pool,
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_recycle=config.pool_recycle_seconds,
            pool_pre_ping=True,
        )
        try:
            yield engine
        finally:
            await engine.dispose()
            log.info("database engine disposed")

    @provide
    def session_factory(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            autoflush=False,
            expire_on_commit=False,
        )

    @provide(scope=Scope.REQUEST)
    async def session(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> AsyncIterable[AnyOf[AsyncSession, TransactionManager]]:
        """One session per request, exposed under two types.

        Repositories ask for ``AsyncSession`` and interactors ask for
        ``TransactionManager``; both resolve to the same object, so the use case
        commits exactly the work its repositories did. Two separate providers
        would hand out two sessions and the commit would apply to neither.
        """
        async with session_factory() as session:
            yield session


class InfrastructureProvider(Provider):
    scope = Scope.APP

    metrics = from_context(provides=MetricsPort)

    @provide
    def identity_provider(self) -> IdentityProvider:
        """Swap this one line for the JWT/JWKS adapter to get real authentication."""
        return DevIdentityProvider()

    @provide
    def clock(self) -> Clock:
        return SystemClock()

    @provide
    def id_generator(self) -> IdGenerator:
        return Uuid7Generator()

    # Each of the four below yields rather than returns. A client that is
    # returned is never closed: dishka has nothing to run at shutdown, and the
    # connections stay open on the Redis side until it times them out. On a
    # rolling deploy that is every replica's pool competing with the replicas
    # replacing it — the same failure the database engine provider avoids the
    # same way.

    @provide
    async def distributed_lock(
        self, config: RedisConfig
    ) -> AsyncIterable[DistributedLockPort]:
        if not config.enabled:
            yield InMemoryDistributedLock()
            return
        from evenkeel.infrastructure.adapters.redis.locking import RedisDistributedLock

        async with _redis_client(config) as client:
            yield RedisDistributedLock(client)

    @provide
    async def rate_limiter(self, config: RedisConfig) -> AsyncIterable[RateLimiterPort]:
        if not config.enabled:
            yield InMemoryRateLimiter()
            return
        from evenkeel.infrastructure.adapters.redis.locking import RedisRateLimiter

        async with _redis_client(config) as client:
            yield RedisRateLimiter(client)

    @provide
    async def bulkhead(self, config: RedisConfig) -> AsyncIterable[BulkheadPort]:
        if not config.enabled:
            yield InMemoryBulkhead()
            return
        from evenkeel.infrastructure.adapters.redis.bulkhead import RedisBulkhead

        async with _redis_client(config) as client:
            yield RedisBulkhead(client)

    @provide
    async def idempotency_store(
        self, config: RedisConfig
    ) -> AsyncIterable[IdempotencyStore]:
        if not config.enabled:
            yield InMemoryIdempotencyStore()
            return
        from evenkeel.infrastructure.adapters.redis.idempotency import (
            RedisIdempotencyStore,
        )

        async with _redis_client(config) as client:
            yield RedisIdempotencyStore(client)

    @provide
    async def risk_assessment(
        self,
        config: RiskConfig,
        app: AppConfig,
        bulkhead: BulkheadPort,
        metrics: MetricsPort,
    ) -> AsyncIterable[RiskAssessmentPort]:
        """The composition root translates configuration into policy.

        All the knob-reading lives here rather than in the adapter, because
        `infrastructure` sits below `setup` in the layer contract. The practical
        payoff is that the adapter can be constructed in a test with three
        explicit policies and no environment at all.
        """
        if config.provider != HTTP_RISK:
            if not app.is_local:
                # Not fatal — a template must run without a vendor account. But
                # a production deployment where every movement is approved by
                # default should say so once, loudly, at boot.
                log.warning("risk_provider_allows_everything", provider=config.provider)
            yield AllowAllRiskAssessment()
            return

        from evenkeel.infrastructure.adapters.http.risk import (
            open_http_risk_assessment,
        )
        from evenkeel.infrastructure.adapters.http.transport import (
            SessionPolicy,
            TransportPolicy,
        )

        http = config.http
        async with open_http_risk_assessment(
            base_url=config.base_url,
            path=config.path,
            api_key=config.api_key.get_secret_value(),
            transport_policy=TransportPolicy(
                service="risk",
                total_timeout_ms=http.total_timeout_ms,
                connect_timeout_ms=http.connect_timeout_ms,
                read_timeout_ms=http.read_timeout_ms,
                budget_ms=http.budget_ms,
                max_attempts=http.max_attempts,
                backoff_base_ms=http.backoff_base_ms,
                backoff_max_ms=http.backoff_max_ms,
                max_response_bytes=http.max_response_bytes,
                bulkhead_limit=http.bulkhead_limit,
                bulkhead_wait_ms=http.bulkhead_wait_ms,
            ),
            session_policy=SessionPolicy(
                connection_limit=http.connection_limit,
                dns_cache_seconds=http.dns_cache_seconds,
                trust_env=http.trust_env,
                backstop_timeout_ms=http.total_timeout_ms,
            ),
            bulkhead=bulkhead,
            metrics=metrics,
        ) as adapter:
            yield adapter


@asynccontextmanager
async def _redis_client(config: RedisConfig) -> AsyncIterator["Redis"]:  # type: ignore[name-defined] # noqa: F821
    """A client with a closing step attached.

    Imported inside the function so the module still loads without the `redis`
    extra — the annotation is a string for the same reason.
    """
    from redis.asyncio import Redis

    client = Redis.from_url(config.url.get_secret_value(), decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()
        log.info("redis client closed")
