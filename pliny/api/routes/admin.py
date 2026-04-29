import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from neo4j import AsyncDriver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.api.deps import get_db, get_neo4j_driver, require_api_key
from pliny.graph.rebuild import rebuild_from_postgres
from pliny.pipeline.stages import STAGE_POOLS, STAGE_VERSIONS
from pliny.schemas.admin import (
    AdminJob,
    AdminJobsResponse,
    JobActionResponse,
    ReprocessStageResponse,
)

router = APIRouter()


_VALID_STATUSES = ("pending", "running", "done", "failed")
# Map stage -> Item column holding the latest successful version. Each stage
# in STAGE_VERSIONS has a matching `<stage>_version` column on `items`.
# Trusted because we only key into this dict via members of STAGE_VERSIONS.
_STAGE_VERSION_COLUMN = {stage: f"{stage}_version" for stage in STAGE_VERSIONS}


@router.post("/rebuild_graph")
async def rebuild_graph(
    _: Annotated[None, Depends(require_api_key)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, int]:
    driver = cast(AsyncDriver, get_neo4j_driver())
    return await rebuild_from_postgres(driver, db)


@router.get("/jobs", response_model=AdminJobsResponse)
async def list_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(require_api_key)],
    job_status: Annotated[str | None, Query(alias="status")] = None,
    stage: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AdminJobsResponse:
    if job_status is not None and job_status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown status: {job_status}"
        )
    if stage is not None and stage not in STAGE_VERSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown stage: {stage}"
        )

    rows = (
        (
            await db.execute(
                text(
                    "SELECT id, item_id, stage, pool, status, attempts, error, "
                    "next_attempt_at, started_at, finished_at "
                    "FROM processing_jobs "
                    "WHERE (CAST(:status_filter AS text) IS NULL OR status = :status_filter) "
                    "  AND (CAST(:stage_filter AS text) IS NULL OR stage = :stage_filter) "
                    "ORDER BY (status = 'failed') DESC, "
                    "         COALESCE(finished_at, started_at, next_attempt_at) DESC NULLS LAST "
                    "LIMIT :limit"
                ),
                {"status_filter": job_status, "stage_filter": stage, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return AdminJobsResponse(jobs=[AdminJob.model_validate(dict(r)) for r in rows])


@router.post("/jobs/{job_id}/retry", response_model=JobActionResponse)
async def retry_job(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(require_api_key)],
) -> JobActionResponse:
    pool = (
        await db.execute(
            text(
                "UPDATE processing_jobs "
                "   SET status='pending', attempts=0, error=NULL, "
                "       claim_token=NULL, next_attempt_at=now(), "
                "       started_at=NULL, finished_at=NULL "
                " WHERE id = :id "
                "RETURNING pool"
            ),
            {"id": job_id},
        )
    ).scalar_one_or_none()
    if pool is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")

    await db.execute(text("SELECT pg_notify(:ch, '')").bindparams(ch=f"job_pool_{pool}"))
    await db.commit()
    return JobActionResponse(status="pending")


@router.post("/reprocess", response_model=ReprocessStageResponse)
async def reprocess_stage(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(require_api_key)],
    stage: Annotated[str, Query()],
) -> ReprocessStageResponse:
    if stage not in STAGE_VERSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown stage: {stage}"
        )

    current = STAGE_VERSIONS[stage]
    pool = STAGE_POOLS[stage]
    column = _STAGE_VERSION_COLUMN[stage]

    # Reset existing job rows for items whose version is behind the constant.
    # Column name comes from a whitelist (STAGE_VERSIONS keys), not user input.
    reset_sql = (
        f"UPDATE processing_jobs "
        f"   SET status='pending', attempts=0, error=NULL, "
        f"       claim_token=NULL, next_attempt_at=now(), "
        f"       started_at=NULL, finished_at=NULL "
        f" WHERE stage = :stage "
        f"   AND item_id IN (SELECT id FROM items WHERE {column} < :current) "
        f"RETURNING id"
    )
    reset_count = len(
        (await db.execute(text(reset_sql), {"stage": stage, "current": current})).all()
    )

    # Insert missing rows for items below the current version.
    insert_sql = (
        f"INSERT INTO processing_jobs "
        f"  (id, item_id, stage, pool, status, attempts, next_attempt_at) "
        f"SELECT gen_random_uuid(), id, :stage, :pool, 'pending', 0, now() "
        f"  FROM items WHERE {column} < :current "
        f"ON CONFLICT (item_id, stage) DO NOTHING "
        f"RETURNING id"
    )
    queued_count = len(
        (
            await db.execute(
                text(insert_sql),
                {"stage": stage, "pool": pool, "current": current},
            )
        ).all()
    )

    if reset_count or queued_count:
        await db.execute(text("SELECT pg_notify(:ch, '')").bindparams(ch=f"job_pool_{pool}"))
    await db.commit()
    return ReprocessStageResponse(reset=reset_count, queued=queued_count)


@router.post("/items/{item_id}/reprocess", response_model=JobActionResponse)
async def reprocess_item_stage(
    item_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(require_api_key)],
    stage: Annotated[str, Query()],
) -> JobActionResponse:
    if stage not in STAGE_VERSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown stage: {stage}"
        )
    pool = STAGE_POOLS[stage]

    item_exists = (
        await db.execute(
            text("SELECT 1 FROM items WHERE id = :id"),
            {"id": item_id},
        )
    ).scalar_one_or_none()
    if item_exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item not found")

    # Idempotent insert; if a row already exists, fall through and reset it.
    await db.execute(
        text(
            "INSERT INTO processing_jobs "
            "  (id, item_id, stage, pool, status, attempts, next_attempt_at) "
            "VALUES (gen_random_uuid(), :id, :stage, :pool, 'pending', 0, now()) "
            "ON CONFLICT (item_id, stage) DO UPDATE SET "
            "  status='pending', attempts=0, error=NULL, claim_token=NULL, "
            "  next_attempt_at=now(), started_at=NULL, finished_at=NULL"
        ),
        {"id": item_id, "stage": stage, "pool": pool},
    )
    await db.execute(text("SELECT pg_notify(:ch, '')").bindparams(ch=f"job_pool_{pool}"))
    await db.commit()
    return JobActionResponse(status="pending")
