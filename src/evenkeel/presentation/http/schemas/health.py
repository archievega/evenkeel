from pydantic import BaseModel, Field


class DependencyStatus(BaseModel):
    healthy: bool = Field(description="Whether the last check of it succeeded.")
    error: str | None = Field(
        default=None,
        description=(
            "The failure class, never the driver's message: a connection error "
            "carries the DSN, and a readiness probe is the last place to leak "
            "one — its output is scraped, logged and alerted on."
        ),
        examples=[None],
    )


class LivenessResponse(BaseModel):
    status: str = Field(
        description=(
            "`alive` whenever the process is running. Deliberately checks "
            "nothing external: a liveness probe that fails during a database "
            "blip restarts every healthy replica and turns a partial outage "
            "into a total one. `/ready` is the one that checks dependencies."
        ),
        examples=["alive"],
    )


class ReadinessResponse(BaseModel):
    status: str = Field(
        description="`ready` or `degraded`. `degraded` is served with a 503.",
        examples=["ready"],
    )
    dependencies: dict[str, DependencyStatus] = Field(
        description="One entry per dependency this instance needs to serve.",
    )


class VersionResponse(BaseModel):
    version: str = Field(
        description="From `APP_VERSION`, or the package version.", examples=["0.1.0"]
    )
    commit: str = Field(
        description=(
            "From `APP_COMMIT`, set at build time. The answer to 'which code is "
            "actually running', which a version alone does not give."
        ),
        examples=["a798e6b"],
    )
    built_at: str = Field(
        description="Image build timestamp, RFC 3339.",
        examples=["2026-08-16T10:00:00+00:00"],
    )
