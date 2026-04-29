from typing import Annotated

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Gauge, generate_latest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.api.deps import get_db

router = APIRouter()


JOBS_BY_POOL_STATUS = Gauge(
    "pliny_processing_jobs",
    "Processing job count by pool and status",
    labelnames=("pool", "status"),
)
STAGE_LAG_SECONDS = Gauge(
    "pliny_stage_lag_seconds",
    "Age of oldest pending or running job per stage (seconds)",
    labelnames=("stage",),
)
STAGE_ERRORS = Gauge(
    "pliny_stage_errors",
    "Failed job count per stage",
    labelnames=("stage",),
)


@router.get("/metrics", include_in_schema=False)
async def metrics(db: Annotated[AsyncSession, Depends(get_db)]) -> Response:
    JOBS_BY_POOL_STATUS.clear()
    STAGE_LAG_SECONDS.clear()
    STAGE_ERRORS.clear()

    pool_status_rows = (
        await db.execute(
            text(
                "SELECT pool, status, count(*)::int AS n FROM processing_jobs GROUP BY pool, status"
            )
        )
    ).mappings()
    for r in pool_status_rows:
        JOBS_BY_POOL_STATUS.labels(pool=r["pool"], status=r["status"]).set(r["n"])

    lag_rows = (
        await db.execute(
            text(
                "SELECT stage, "
                "       EXTRACT(EPOCH FROM (now() - "
                "       min(COALESCE(started_at, next_attempt_at))))::float AS lag "
                "FROM processing_jobs "
                "WHERE status IN ('pending','running') "
                "GROUP BY stage"
            )
        )
    ).mappings()
    for r in lag_rows:
        STAGE_LAG_SECONDS.labels(stage=r["stage"]).set(float(r["lag"] or 0.0))

    error_rows = (
        await db.execute(
            text(
                "SELECT stage, count(*)::int AS n FROM processing_jobs "
                "WHERE status='failed' GROUP BY stage"
            )
        )
    ).mappings()
    for r in error_rows:
        STAGE_ERRORS.labels(stage=r["stage"]).set(r["n"])

    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
