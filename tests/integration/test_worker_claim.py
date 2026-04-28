import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pliny.api import deps
from pliny.db.models import Item, ProcessingJob
from pliny.db.queries import enqueue_job, insert_item
from pliny.pipeline import stages as stage_registry
from pliny.pipeline.context import StageContext
from pliny.workers.runner import (
    ClaimedJob,
    claim_one,
    mark_done_and_enqueue_downstream,
    record_failure,
    run_one_job,
    unpark_claim,
)
from pliny.workers.sweeper import sweep_once


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def _insert_pending_text(db_session: AsyncSession) -> uuid.UUID:
    item = await insert_item(
        db_session,
        type="text",
        content_hash=uuid.uuid4().hex,
    )
    await enqueue_job(db_session, item_id=item.id, stage="extract", pool="fast")
    await db_session.commit()
    return item.id


async def test_claim_returns_pending_job(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    await _insert_pending_text(db_session)

    sm = deps.get_session_maker()
    async with sm() as s:
        claimed = await claim_one(s, "fast")
    assert claimed is not None
    assert claimed.stage == "extract"
    assert claimed.attempts == 1


async def test_claim_returns_none_when_empty(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    sm = deps.get_session_maker()
    async with sm() as s:
        claimed = await claim_one(s, "fast")
    assert claimed is None


async def test_two_concurrent_claims_only_one_wins(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    await _insert_pending_text(db_session)

    sm = deps.get_session_maker()

    async def _claim() -> ClaimedJob | None:
        async with sm() as s:
            return await claim_one(s, "fast")

    a, b = await asyncio.gather(_claim(), _claim())
    winners = [c for c in (a, b) if c is not None]
    assert len(winners) == 1


async def test_mark_done_with_matching_token_succeeds(
    db_session: AsyncSession,
) -> None:
    await _truncate(db_session)
    item_id = await _insert_pending_text(db_session)

    sm = deps.get_session_maker()
    async with sm() as s:
        claimed = await claim_one(s, "fast")
    assert claimed is not None

    async with sm() as s:
        ok = await mark_done_and_enqueue_downstream(s, claimed=claimed, item_type="text")
        await s.commit()
    assert ok is True

    job = (
        await db_session.execute(
            select(ProcessingJob).where(
                ProcessingJob.item_id == item_id, ProcessingJob.stage == "extract"
            )
        )
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "done"
    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.extract_version == stage_registry.STAGE_VERSIONS["extract"]

    downstream_count = (
        await db_session.execute(
            text(
                "SELECT count(*)::int FROM processing_jobs "
                "WHERE item_id = :id AND stage IN ('summarize', 'chunk')"
            ),
            {"id": item_id},
        )
    ).scalar_one()
    assert downstream_count == 2


async def test_mark_done_with_clobbered_token_noops(
    db_session: AsyncSession,
) -> None:
    await _truncate(db_session)
    item_id = await _insert_pending_text(db_session)

    sm = deps.get_session_maker()
    async with sm() as s:
        claimed = await claim_one(s, "fast")
    assert claimed is not None

    # Simulate a reprocess clobber: NULL the claim_token mid-flight
    await db_session.execute(
        text(
            "UPDATE processing_jobs SET claim_token = NULL "
            "WHERE item_id = :id AND stage = 'extract'"
        ),
        {"id": item_id},
    )
    await db_session.commit()

    async with sm() as s:
        ok = await mark_done_and_enqueue_downstream(s, claimed=claimed, item_type="text")
        await s.commit()
    assert ok is False


async def test_record_failure_pending_with_backoff(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    await _insert_pending_text(db_session)

    sm = deps.get_session_maker()
    async with sm() as s:
        claimed = await claim_one(s, "fast")
    assert claimed is not None

    async with sm() as s:
        status = await record_failure(s, claimed=claimed, error="boom")
        await s.commit()
    assert status == "pending"

    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.id == claimed.job_id))
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.next_attempt_at is not None
    assert job.error == "boom"


async def test_record_failure_after_max_attempts(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    item_id = await _insert_pending_text(db_session)

    # Bypass repeated claim/fail cycles: bump attempts to 6 directly
    await db_session.execute(
        text(
            "UPDATE processing_jobs SET status='running', attempts=6, "
            "claim_token=gen_random_uuid(), started_at=now() "
            "WHERE item_id = :id AND stage = 'extract'"
        ),
        {"id": item_id},
    )
    await db_session.commit()
    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.item_id == item_id))
    ).scalar_one()
    await db_session.refresh(job)
    claimed = ClaimedJob(
        job_id=job.id,
        item_id=job.item_id,
        stage=job.stage,
        attempts=job.attempts,
        claim_token=job.claim_token,  # type: ignore[arg-type]
    )

    sm = deps.get_session_maker()
    async with sm() as s:
        status = await record_failure(s, claimed=claimed, error="give up")
        await s.commit()
    assert status == "failed"


async def test_sweeper_resets_stale_running(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    item_id = await _insert_pending_text(db_session)
    await db_session.execute(
        text(
            "UPDATE processing_jobs SET status='running', "
            "started_at=now() - interval '30 minutes', "
            "claim_token=gen_random_uuid() "
            "WHERE item_id = :id"
        ),
        {"id": item_id},
    )
    await db_session.commit()

    sm = deps.get_session_maker()
    async with sm() as s:
        reset = await sweep_once(s, stage_timeout_seconds=900)
    assert reset == 1

    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.item_id == item_id))
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.claim_token is None


async def test_sweeper_skips_recent_running(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    item_id = await _insert_pending_text(db_session)
    await db_session.execute(
        text(
            "UPDATE processing_jobs SET status='running', "
            "started_at=now() - interval '5 minutes', "
            "claim_token=gen_random_uuid() "
            "WHERE item_id = :id"
        ),
        {"id": item_id},
    )
    await db_session.commit()

    sm = deps.get_session_maker()
    async with sm() as s:
        reset = await sweep_once(s, stage_timeout_seconds=900)
    assert reset == 0


async def test_unpark_claim_returns_to_pending(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    await _insert_pending_text(db_session)
    sm = deps.get_session_maker()
    async with sm() as s:
        claimed = await claim_one(s, "fast")
    assert claimed is not None
    async with sm() as s:
        await unpark_claim(s, claimed)
    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.id == claimed.job_id))
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.claim_token is None


async def test_run_one_job_with_stub_handler(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    item_id = await _insert_pending_text(db_session)

    called: list[StageContext] = []

    @stage_registry.register("extract")
    async def _stub(ctx: StageContext) -> None:
        called.append(ctx)

    sm = deps.get_session_maker()
    try:
        ran = await run_one_job(
            sm=sm,
            pool_name="fast",
            blob=deps.get_blob(),
            llm=None,
        )
    finally:
        # Restore: leave registry without a handler so other tests aren't affected
        stage_registry._HANDLERS.pop("extract", None)

    assert ran is True
    assert len(called) == 1
    assert called[0].item_id == item_id

    job = (
        await db_session.execute(
            select(ProcessingJob).where(
                ProcessingJob.item_id == item_id, ProcessingJob.stage == "extract"
            )
        )
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "done"
    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.extract_version == stage_registry.STAGE_VERSIONS["extract"]


_ = (datetime, timedelta, timezone, async_sessionmaker)  # silence unused warnings
