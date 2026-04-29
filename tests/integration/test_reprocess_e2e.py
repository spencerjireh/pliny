"""End-to-end reprocessing flow validation.

Bumps a stage version, calls the admin reprocess endpoint, runs the worker
once via the in-process runner, and asserts the item's stage version reaches
the new constant. Also covers the single-item reprocess path with a stub
handler. LLM and Neo4j are stubbed at the wrapper boundary; the worker uses
real Postgres via testcontainers.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.api import deps
from pliny.db.models import Item, ProcessingJob
from pliny.db.queries import enqueue_job, insert_item
from pliny.pipeline import stages as stage_registry
from pliny.pipeline.context import StageContext
from pliny.workers.runner import run_one_job


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def test_bulk_reprocess_drives_item_to_new_version(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """POST /admin/reprocess?stage=summarize after a version bump:
    1) resets matching jobs to pending,
    2) the worker re-runs the handler,
    3) items.summarize_version lands at the new constant.
    """
    await _truncate(db_session)

    item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    await db_session.execute(
        text("UPDATE items SET summarize_version=1 WHERE id=:id"),
        {"id": item.id},
    )
    await enqueue_job(db_session, item_id=item.id, stage="summarize", pool="fast")
    await db_session.execute(
        text("UPDATE processing_jobs SET status='done' WHERE item_id=:id"),
        {"id": item.id},
    )
    await db_session.commit()

    from pliny.api.routes import admin as admin_mod

    monkeypatch.setitem(stage_registry.STAGE_VERSIONS, "summarize", 2)
    monkeypatch.setitem(admin_mod.STAGE_VERSIONS, "summarize", 2)

    r = await client.post("/v1/admin/reprocess?stage=summarize", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"reset": 1, "queued": 0}

    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.item_id == item.id))
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "pending"

    called: list[StageContext] = []

    async def _stub(ctx: StageContext) -> None:
        called.append(ctx)

    stage_registry._HANDLERS["summarize"] = _stub
    try:
        ran = await run_one_job(
            sm=deps.get_session_maker(),
            pool_name="fast",
            blob=deps.get_blob(),
            llm=None,
        )
    finally:
        stage_registry._HANDLERS.pop("summarize", None)

    assert ran is True
    assert len(called) == 1
    assert called[0].item_id == item.id

    refreshed = (await db_session.execute(select(Item).where(Item.id == item.id))).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.summarize_version == 2

    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.item_id == item.id))
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "done"


async def test_per_item_reprocess_drives_extract_back_to_done(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """POST /admin/items/:id/reprocess?stage=extract resets one stage; the worker
    re-runs the handler. Doesn't bump version constants."""
    await _truncate(db_session)
    item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    await db_session.commit()

    r = await client.post(
        f"/v1/admin/items/{item.id}/reprocess?stage=extract", headers=auth_headers
    )
    assert r.status_code == 200

    called: list[StageContext] = []

    async def _stub(ctx: StageContext) -> None:
        called.append(ctx)

    stage_registry._HANDLERS["extract"] = _stub
    try:
        ran = await run_one_job(
            sm=deps.get_session_maker(),
            pool_name="fast",
            blob=deps.get_blob(),
            llm=None,
        )
    finally:
        stage_registry._HANDLERS.pop("extract", None)

    assert ran is True
    assert len(called) == 1
    assert called[0].item_id == item.id

    job = (
        await db_session.execute(
            select(ProcessingJob).where(
                ProcessingJob.item_id == item.id, ProcessingJob.stage == "extract"
            )
        )
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "done"

    refreshed = (await db_session.execute(select(Item).where(Item.id == item.id))).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.extract_version == stage_registry.STAGE_VERSIONS["extract"]


async def test_retry_then_run_recovers_failed_job(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """Failed job → POST /admin/jobs/:id/retry → worker claims it → done."""
    await _truncate(db_session)
    item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    await enqueue_job(db_session, item_id=item.id, stage="extract", pool="fast")
    await db_session.execute(
        text(
            "UPDATE processing_jobs SET status='failed', error='boom', "
            "attempts=6 WHERE item_id=:id AND stage='extract'"
        ),
        {"id": item.id},
    )
    await db_session.commit()

    job_id = (
        await db_session.execute(
            select(ProcessingJob.id).where(
                ProcessingJob.item_id == item.id, ProcessingJob.stage == "extract"
            )
        )
    ).scalar_one()

    r = await client.post(f"/v1/admin/jobs/{job_id}/retry", headers=auth_headers)
    assert r.status_code == 200

    async def _stub(ctx: StageContext) -> None:
        pass

    stage_registry._HANDLERS["extract"] = _stub
    try:
        ran = await run_one_job(
            sm=deps.get_session_maker(),
            pool_name="fast",
            blob=deps.get_blob(),
            llm=None,
        )
    finally:
        stage_registry._HANDLERS.pop("extract", None)

    assert ran is True

    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.id == job_id))
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "done"
    assert job.error is None
