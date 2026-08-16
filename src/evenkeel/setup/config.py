from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

LOCAL = "local"
DEV_IDENTITY = "dev"
ALLOW_ALL_RISK = "allow-all"
HTTP_RISK = "http"

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


class MovementPolicyConfig(BaseModel):
    """Policy for balance changes, in configuration rather than in code.

    These were literals inside `MoveMoneySettings` with no way to reach them
    from the outside, which is the sort of default that survives until the first
    incident and is then changed by a deploy of the application. A limit that
    cannot be tuned without a release is not a control, it is a constant.
    """

    rate_limit_enabled: bool = True
    # Per owner, not per IP. An IP is shared by a corporate NAT and rented by
    # anyone who wants a second one; the owner id is the thing being protected.
    rate_limit_limit: int = 30
    rate_limit_window_seconds: float = 60.0
    risk_fail_open: bool = False


class OutboundHttpConfig(BaseModel):
    """How this service calls someone else's.

    Defaults are deliberately impatient. A dependency on the request path gets
    one retry and a budget under two seconds, because the alternative — a
    generous timeout that "gives it a chance" — converts the provider's bad day
    into this service's outage, one held connection at a time.
    """

    total_timeout_ms: int = 800
    connect_timeout_ms: int = 200
    read_timeout_ms: int = 700
    # Wall clock for the whole call including retries and backoff. This, not
    # the per-attempt timeout, is the number to quote when someone asks what
    # the worst case is.
    budget_ms: int = 1_500
    max_attempts: int = 2
    backoff_base_ms: int = 50
    backoff_max_ms: int = 400
    max_response_bytes: int = 64 * 1024
    # Concurrent calls allowed in flight. Sized against the database pool, not
    # against the provider: the number that matters is how many request-scoped
    # connections may be parked waiting on someone else's service.
    bulkhead_limit: int = 32
    # Zero means refuse immediately rather than queue. Queueing for a slot is
    # the failure mode a bulkhead exists to prevent.
    bulkhead_wait_ms: int = 0
    connection_limit: int = 64
    dns_cache_seconds: int = 30
    # Off by default: HTTP_PROXY in the environment silently rerouting outbound
    # traffic is a supply-chain risk, not a feature.
    trust_env: bool = False


class RiskConfig(BaseModel):
    """The one external decision on the money path.

    `allow-all` is the default so the template runs with no vendor account.
    Setting `provider=http` and a `base_url` is the whole switch.

    `fail_open` is the question this config exists to make someone answer: when
    the provider cannot be reached, does money move unchecked or does it stop?
    Closed by default — an unverified movement is permanent, and a 503 is not.
    """

    provider: str = ALLOW_ALL_RISK
    base_url: str = ""
    path: str = "/decisions"
    api_key: SecretStr = SecretStr("")
    fail_open: bool = False
    http: OutboundHttpConfig = Field(default_factory=OutboundHttpConfig)

    @model_validator(mode="after")
    def _http_needs_a_destination(self) -> "RiskConfig":
        # Caught at load time rather than on the first movement. A misconfigured
        # provider that only announces itself under traffic is a deploy that
        # looks successful and is not.
        if self.provider == HTTP_RISK and not self.base_url:
            raise ValueError("risk.base_url is required when risk.provider is 'http'")
        return self


class McpConfig(BaseModel):
    """The MCP transport acts on behalf of exactly one owner.

    `owner_id` is configuration and never a tool argument. A tool that took the
    owner would be a cross-tenant IDOR with a natural-language interface: the
    model reads untrusted text for a living, and anything that persuades it to
    pass a different id moves somebody else's money.

    Empty by default and refused at boot rather than defaulted, for the same
    reason `identity_provider` is: an authorisation decision nobody made is not
    a safe one.
    """

    owner_id: str = ""


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
    risk: RiskConfig = Field(default_factory=RiskConfig)
    movements: MovementPolicyConfig = Field(default_factory=MovementPolicyConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
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
    if settings.risk.provider == HTTP_RISK and settings.risk.base_url.startswith(
        "http://"
    ):
        # Not pedantry: this request carries an owner id, an amount and a bearer
        # token to a third party. Plaintext makes all three readable by every
        # hop in between, and the token replayable.
        problems.append(
            "risk.base_url is plaintext http, and the request carries a token"
        )
    return problems
