import uuid

from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.db.models import ProcessingJob
from pliny.db.queries import enqueue_job, insert_item


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def _seed_job(
    db_session: AsyncSession,
    *,
    stage: str = "extract",
    pool: str = "fast",
    job_status: str = "pending",
    error: str | None = None,
    attempts: int = 0,
) -> tuple[uuid.UUID, uuid.UUID]:
    item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    await enqueue_job(db_session, item_id=item.id, stage=stage, pool=pool)
    await db_session.execute(
        text(
            "UPDATE processing_jobs SET status=:s, attempts=:a, error=:e "
            "WHERE item_id=:id AND stage=:stage"
        ),
        {"s": job_status, "a": attempts, "e": error, "id": item.id, "stage": stage},
    )
    await db_session.commit()
    job_id = (
        await db_session.execute(
            select(ProcessingJob.id).where(
                ProcessingJob.item_id == item.id, ProcessingJob.stage == stage
            )
        )
    ).scalar_one()
    return item.id, job_id


async def test_list_jobs_empty(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    r = await client.get("/v1/admin/jobs", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"jobs": []}


async def test_list_jobs_filters_status(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    await _seed_job(db_session, job_status="failed", error="boom", attempts=6)
    await _seed_job(db_session, job_status="pending")

    r = await client.get("/v1/admin/jobs?status=failed", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["status"] == "failed"
    assert body["jobs"][0]["error"] == "boom"
    assert body["jobs"][0]["attempts"] == 6


async def test_list_jobs_filters_stage(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    await _seed_job(db_session, stage="extract")
    await _seed_job(db_session, stage="summarize")

    r = await client.get("/v1/admin/jobs?stage=summarize", headers=auth_headers)
    assert r.status_code == 200
    stages = [j["stage"] for j in r.json()["jobs"]]
    assert stages == ["summarize"]


async def test_list_jobs_limit_clamped(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    r = await client.get("/v1/admin/jobs?limit=0", headers=auth_headers)
    assert r.status_code == 422
    r = await client.get("/v1/admin/jobs?limit=501", headers=auth_headers)
    assert r.status_code == 422


async def test_list_jobs_rejects_unknown_status(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    r = await client.get("/v1/admin/jobs?status=bogus", headers=auth_headers)
    assert r.status_code == 400


async def test_list_jobs_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/admin/jobs")
    assert r.status_code == 401


async def test_retry_job_resets_failed_row(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    item_id, job_id = await _seed_job(db_session, job_status="failed", error="boom", attempts=6)
    await db_session.execute(
        text(
            "UPDATE processing_jobs SET claim_token=gen_random_uuid(), "
            "started_at=now() WHERE id=:id"
        ),
        {"id": job_id},
    )
    await db_session.commit()

    r = await client.post(f"/v1/admin/jobs/{job_id}/retry", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"status": "pending"}

    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.id == job_id))
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.error is None
    assert job.claim_token is None
    assert job.started_at is None
    assert job.next_attempt_at is not None
    _ = item_id


async def test_retry_job_404_when_missing(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    r = await client.post(f"/v1/admin/jobs/{uuid.uuid4()}/retry", headers=auth_headers)
    assert r.status_code == 404


async def test_retry_job_requires_auth(client: AsyncClient) -> None:
    r = await client.post(f"/v1/admin/jobs/{uuid.uuid4()}/retry")
    assert r.status_code == 401
