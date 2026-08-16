from collections.abc import AsyncIterable

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
from evenkeel.infrastructure.adapters.dev_identity import DevIdentityProvider
from evenkeel.infrastructure.adapters.memory.bulkhead import InMemoryBulkhead
from evenkeel.infrastructure.adapters.memory.idempotency import InMemoryIdempotencyStore
from evenkeel.infrastructure.adapters.memory.locking import (
    InMemoryDistributedLock,
    InMemoryRateLimiter,
)
from evenkeel.infrastructure.adapters.system import SystemClock, Uuid7Generator
from evenkeel.logging import get_logger
from evenkeel.setup.config import (
    AppConfig,
    DatabaseConfig,
    ObservabilityConfig,
    RedisConfig,
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

    @provide
    def distributed_lock(self, config: RedisConfig) -> DistributedLockPort:
        if not config.enabled:
            return InMemoryDistributedLock()
        from redis.asyncio import Redis

        from evenkeel.infrastructure.adapters.redis.locking import RedisDistributedLock

        return RedisDistributedLock(
            Redis.from_url(config.url.get_secret_value(), decode_responses=True)
        )

    @provide
    def rate_limiter(self, config: RedisConfig) -> RateLimiterPort:
        if not config.enabled:
            return InMemoryRateLimiter()
        from redis.asyncio import Redis

        from evenkeel.infrastructure.adapters.redis.locking import RedisRateLimiter

        return RedisRateLimiter(
            Redis.from_url(config.url.get_secret_value(), decode_responses=True)
        )

    @provide
    def bulkhead(self, config: RedisConfig) -> BulkheadPort:
        if not config.enabled:
            return InMemoryBulkhead()
        from redis.asyncio import Redis

        from evenkeel.infrastructure.adapters.redis.bulkhead import RedisBulkhead

        return RedisBulkhead(
            Redis.from_url(config.url.get_secret_value(), decode_responses=True)
        )

    @provide
    def idempotency_store(self, config: RedisConfig) -> IdempotencyStore:
        if not config.enabled:
            return InMemoryIdempotencyStore()
        from redis.asyncio import Redis

        from evenkeel.infrastructure.adapters.redis.idempotency import (
            RedisIdempotencyStore,
        )

        return RedisIdempotencyStore(
            Redis.from_url(config.url.get_secret_value(), decode_responses=True)
        )
