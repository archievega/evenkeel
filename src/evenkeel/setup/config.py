from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET = "change-me"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    json_logs: bool = True
    console_pretty_in_debug: bool = True
    include_timestamp: bool = True


class ApiConfig(BaseModel):
    prefix: str = "/v1"
    wallets: str = "/wallets"


class AppConfig(BaseModel):
    name: str = "evenkeel"
    environment: str = "local"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
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
        return SecretStr(
            f"postgresql+asyncpg://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @property
    def sync_dsn(self) -> SecretStr:
        return SecretStr(
            f"postgresql+psycopg://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.database}"
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
    """
    problems: list[str] = []
    if settings.app.debug:
        problems.append("app.debug is enabled")
    secret = settings.app.secret_key.get_secret_value()
    if not secret or secret == DEFAULT_SECRET:
        problems.append("app.secret_key is empty or still the public default")
    if settings.database.password.get_secret_value() in {"", "evenkeel", "postgres"}:
        problems.append("database.password is empty or a well-known default")
    return problems
