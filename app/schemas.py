from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_final(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    running_jobs: int


class JobParameters(BaseModel):
    # Só parâmetros declarados; qualquer outro vira 422.
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1, le=10000, strict=True)


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: str = Field(min_length=1, max_length=64)
    execution_id: str = Field(min_length=1, max_length=64)
    parameters: JobParameters = Field(default_factory=JobParameters)


class JobCreatedResponse(BaseModel):
    job_id: str
    execution_id: str
    status: JobStatus


class JobResponse(BaseModel):
    job_id: str
    execution_id: str
    script: str
    status: JobStatus
    started_at: str | None
    finished_at: str | None
    metrics: dict[str, Any]
    log_tail: list[str]
    error: str | None
