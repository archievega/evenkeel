from pydantic import BaseModel


class DependencyStatus(BaseModel):
    healthy: bool
    error: str | None = None


class LivenessResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    dependencies: dict[str, DependencyStatus]


class VersionResponse(BaseModel):
    version: str
    commit: str
    built_at: str
