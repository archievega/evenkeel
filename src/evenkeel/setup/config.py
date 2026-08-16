from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

LOCAL = "local"
DEV_IDENTITY = "dev"

# nosec B105 — the opposite of a hardcoded credential. This is the sentinel the
# boot check compares against: seeing it means nobody supplied a secret, and the
# server refuses to start in production. Removing it would remove the guard, not
# the risk.
DEFAULT_SECRET = "change-me"  # nosec B105


class LoggingConfig(BaseModel):
    level: str = "INFO"
    json_logs: bool = True
    console_pretty_in_debug: bool = True
    include_timestamp: bool = True


class ApiConfig(BaseModel):
    prefix: str = "/v1"
    wallets: str = "/wallets"


class AppConfig(BaseModel):
    """One switch, and it fails closed.

    There used to be two — `debug` and `environment` — and they interacted
    badly: `debug` defaulted to True, it was reported as a fatal problem by the
    boot guard, and it was also the flag that suppressed the guard, so the check
    could never fire in the configuration that shipped. Meanwhile `environment`
    was read by nothing.

    `environment` now decides everything and defaults to `production`. An image
    deployed without configuration therefore gets the strict posture, and the
    permissive one has to be asked for — which is what `compose.yml` and
    `.env.example` do explicitly.
    """

    name: str = "evenkeel"
    environment: str = "production"
    # The only identity adapter that exists. Named in configuration so the boot
    # guard has something to refuse: a placeholder that authenticates whoever
    # asks must not reach production silently.
    identity_provider: str = DEV_IDENTITY
    # nosec B104 — a container that binds loopback is unreachable from outside
    # itself, so 0.0.0.0 is the only workable default here. Exposure is
    # controlled one layer out: compose publishes to 127.0.0.1 only, and in
    # production the container sits behind a reverse proxy.
    host: str = "0.0.0.0"  # nosec B104
    port: int = 8000
    workers: int = 1
    # Off by default, and only honoured with a single worker. Beyond the usual
    # "reload is for development", uvicorn's watcher has a failure mode that
    # costs real debugging time: on rapid successive reloads the shutdown half
    # of the lifespan sometimes does not run. Ours closes the DI container,
    # which disposes the SQLAlchemy pool — so a skipped shutdown leaks
    # connections, and the symptom appears far from the cause.
    reload: bool = False
    # Idle upstream connections must be closed by the reverse proxy, not by the
    # app: if the app closes first, in-flight requests surface to clients as
    # "upstream prematurely closed connection". Keep this above the proxy value.
    timeout_keep_alive: int = 75
    # How long the server waits for in-flight requests after SIGTERM. Must be
    # shorter than the orchestrator's kill grace period, or the process is
    # SIGKILLed mid-request and the client sees a connection reset instead of a
    # response it already paid for.
    timeout_graceful_shutdown: int = 30
    secret_key: SecretStr = SecretStr(DEFAULT_SECRET)
    api: ApiConfig = Field(default_factory=ApiConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def is_local(self) -> bool:
        return self.environment == LOCAL


class DatabaseConfig(BaseModel):
    host: str = "localhost"
    port: int = 5432
    user: str = "evenkeel"
    password: SecretStr = SecretStr("evenkeel")
    database: str = "evenkeel"
    echo: bool = False
    echo_pool: bool = False
    pool_size: int = 10
    max_overflow: int = 20
    # Recycle below any proxy/database idle timeout, otherwise the pool hands
    # out connections the server has already closed and the first query of a
    # quiet period fails.
    pool_recycle_seconds: int = 1800

    @property
    def async_dsn(self) -> SecretStr:
        """Built component-wise, never by f-string.

        An f-string DSN treats the password as URL syntax. `p@ss` makes the
        parser read `ss@host` as the host and connect somewhere else with a
        truncated credential; `pa%ss` detonates later inside alembic's
        ConfigParser, printing the whole DSN to stderr. `URL.create` escapes
        each component, so a generated password stays a password.
        """
        return SecretStr(
            URL.create(
                drivername="postgresql+asyncpg",
                username=self.user,
                password=self.password.get_secret_value(),
                host=self.host,
                port=self.port,
                database=self.database,
            ).render_as_string(hide_password=False)
        )


class RedisConfig(BaseModel):
    """Optional. Empty ``url`` keeps the in-memory lock/limiter/idempotency adapters."""

    url: SecretStr = SecretStr("")

    @property
    def enabled(self) -> bool:
        return bool(self.url.get_secret_value())


class ObservabilityConfig(BaseModel):
    metrics_enabled: bool = False
    tracing_enabled: bool = False
    otlp_endpoint: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        env_prefix="APP__",
        extra="ignore",
    )

    app: AppConfig = Field(default_factory=AppConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


def load_settings() -> Settings:
    """Read configuration explicitly.

    Deliberately a function, not a module-level singleton: a singleton reads
    the environment at import time, so importing anything in a test fixes the
    configuration before the test can change it, and an invalid environment
    fails during collection with an import error instead of a clear message.
    """
    return Settings()


def production_config_problems(settings: Settings) -> list[str]:
    """Configuration that is survivable locally and unacceptable in production.

    Returned rather than raised so the caller decides: the server entrypoint
    refuses to boot, while tests and tooling can build an app without it.

    Note what is NOT checked here: `environment` itself. This function answers
    "is this configuration safe to expose", and the caller answers "does that
    matter here". Checking the environment in both places is how the previous
    version ended up suppressing itself.
    """
    problems: list[str] = []
    if settings.app.identity_provider == DEV_IDENTITY:
        problems.append(
            "app.identity_provider is still 'dev', which authenticates anyone "
            "who supplies an owner id"
        )
    secret = settings.app.secret_key.get_secret_value()
    if not secret or secret == DEFAULT_SECRET:
        problems.append("app.secret_key is empty or still the public default")
    if settings.database.password.get_secret_value() in {"", "evenkeel", "postgres"}:
        problems.append("database.password is empty or a well-known default")
    return problems
