from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AdminJob(BaseModel):
    id: UUID
    item_id: UUID
    stage: str
    pool: str
    status: str
    attempts: int
    error: str | None
    next_attempt_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class AdminJobsResponse(BaseModel):
    jobs: list[AdminJob]


class ReprocessStageResponse(BaseModel):
    reset: int
    queued: int


class JobActionResponse(BaseModel):
    status: str
