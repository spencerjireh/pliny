import time
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pliny.db.queries import enqueue_job, notify
from pliny.logging import bind, get_logger
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import (
    STAGE_POOLS,
    STAGE_PREREQS,
    STAGE_VERSIONS,
    NoHandlerError,
    downstream_stages,
    get_handler,
)
from pliny.storage.blob import BlobStore
from pliny.workers.retry import MAX_ATTEMPTS, next_delay_seconds


@dataclass
class ClaimedJob:
    job_id: uuid.UUID
    item_id: uuid.UUID
    stage: str
    attempts: int
    claim_token: uuid.UUID


async def claim_one(session: AsyncSession, pool_name: str) -> ClaimedJob | None:
    """Atomically claim one pending job for `pool_name`. Commits on success."""
    candidate = (
        await session.execute(
            text(
                """
                SELECT id FROM processing_jobs
                 WHERE status = 'pending'
                   AND pool = :pool
                   AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                 ORDER BY next_attempt_at NULLS FIRST
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
                """
            ),
            {"pool": pool_name},
        )
    ).scalar_one_or_none()
    if candidate is None:
        await session.commit()
        return None

    row = (
        (
            await session.execute(
                text(
                    """
                UPDATE processing_jobs
                   SET status='running',
                       started_at=now(),
                       attempts=attempts + 1,
                       claim_token=gen_random_uuid()
                 WHERE id=:id AND status='pending'
                RETURNING id, item_id, stage, attempts, claim_token
                """
                ),
                {"id": candidate},
            )
        )
        .mappings()
        .one_or_none()
    )
    await session.commit()
    if row is None:
        return None
    return ClaimedJob(
        job_id=row["id"],
        item_id=row["item_id"],
        stage=row["stage"],
        attempts=row["attempts"],
        claim_token=row["claim_token"],
    )


async def _prereqs_satisfied(session: AsyncSession, *, item_id: uuid.UUID, stage: str) -> bool:
    deps = STAGE_PREREQS.get(stage, [])
    if not deps:
        return True
    cols = ", ".join(f"{d}_version" for d in deps)
    row = (
        (
            await session.execute(
                text(f"SELECT {cols} FROM items WHERE id = :id"),
                {"id": item_id},
            )
        )
        .mappings()
        .one()
    )
    return all(row[f"{d}_version"] >= STAGE_VERSIONS[d] for d in deps)


async def mark_done_and_enqueue_downstream(
    session: AsyncSession,
    *,
    claimed: ClaimedJob,
    item_type: str,
) -> bool:
    """Mark the job done, bump items.<stage>_version, enqueue downstream.

    Returns True if the row was actually updated; False if claim_token mismatched
    (meaning a reprocess or sweeper invalidated us mid-flight).
    """
    version = STAGE_VERSIONS.get(claimed.stage, 0)
    result = await session.execute(
        text(
            """
            UPDATE processing_jobs
               SET status='done', finished_at=now(), error=NULL
             WHERE id=:id AND claim_token=:token
            """
        ),
        {"id": claimed.job_id, "token": str(claimed.claim_token)},
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        return False

    version_col = f"{claimed.stage}_version"
    await session.execute(
        text(f"UPDATE items SET {version_col} = :v WHERE id = :id"),
        {"v": version, "id": claimed.item_id},
    )

    for next_stage in downstream_stages(item_type, claimed.stage):
        if not await _prereqs_satisfied(session, item_id=claimed.item_id, stage=next_stage):
            continue
        pool = STAGE_POOLS[next_stage]
        enqueued = await enqueue_job(session, item_id=claimed.item_id, stage=next_stage, pool=pool)
        if enqueued:
            await notify(session, f"job_pool_{pool}", str(claimed.item_id))

    return True


async def record_failure(
    session: AsyncSession,
    *,
    claimed: ClaimedJob,
    error: str,
) -> str:
    """Decide retry vs. fail and persist. Returns final status: 'pending' or 'failed'."""
    delay = next_delay_seconds(claimed.attempts)
    truncated = error[:1000]
    if delay is None:
        await session.execute(
            text(
                """
                UPDATE processing_jobs
                   SET status='failed', error=:err, next_attempt_at=NULL,
                       finished_at=now()
                 WHERE id=:id AND claim_token=:token
                """
            ),
            {"id": claimed.job_id, "token": str(claimed.claim_token), "err": truncated},
        )
        return "failed"
    await session.execute(
        text(
            """
            UPDATE processing_jobs
               SET status='pending', error=:err,
                   next_attempt_at=now() + make_interval(secs => :secs)
             WHERE id=:id AND claim_token=:token
            """
        ),
        {
            "id": claimed.job_id,
            "token": str(claimed.claim_token),
            "err": truncated,
            "secs": delay,
        },
    )
    return "pending"


async def unpark_claim(session: AsyncSession, claimed: ClaimedJob) -> None:
    """Used on graceful shutdown: return the row to pending if we still hold the claim."""
    await session.execute(
        text(
            """
            UPDATE processing_jobs
               SET status='pending', claim_token=NULL, next_attempt_at=now()
             WHERE id=:id AND claim_token=:token
            """
        ),
        {"id": claimed.job_id, "token": str(claimed.claim_token)},
    )
    await session.commit()


async def run_one_job(
    *,
    sm: async_sessionmaker[AsyncSession],
    pool_name: str,
    blob: BlobStore,
    llm: object | None,
    neo4j: object | None = None,
    on_no_job_log: bool = False,
) -> bool:
    """Claim and run one job. Returns True if a job was processed."""
    logger = get_logger("pliny.workers.runner")

    async with sm() as session:
        claimed = await claim_one(session, pool_name)
    if claimed is None:
        return False

    log = bind(
        logger,
        stage=claimed.stage,
        item_id=str(claimed.item_id),
        attempt=claimed.attempts,
        claim_token=str(claimed.claim_token),
    )

    started = time.monotonic()
    try:
        async with sm() as session:
            item_type = (
                await session.execute(
                    text("SELECT type FROM items WHERE id = :id"),
                    {"id": claimed.item_id},
                )
            ).scalar_one_or_none()
            if item_type is None:
                # Item disappeared (e.g. cascade-deleted). Nothing to do.
                log.warning("item_disappeared")
                return True

            ctx = StageContext(
                item_id=claimed.item_id,
                stage=claimed.stage,
                attempt=claimed.attempts,
                claim_token=claimed.claim_token,
                db=session,
                blob=blob,
                llm=llm,  # type: ignore[arg-type]
                logger=log,
                neo4j=neo4j,
            )
            handler = get_handler(claimed.stage)
            await handler(ctx)
            updated = await mark_done_and_enqueue_downstream(
                session, claimed=claimed, item_type=item_type
            )
            await session.commit()
            if updated:
                log.info(
                    "stage_done",
                    extra={"latency_ms": int((time.monotonic() - started) * 1000)},
                )
            else:
                log.info("stage_done_clobbered")
    except NoHandlerError as exc:
        async with sm() as session:
            await session.execute(
                text(
                    "UPDATE processing_jobs SET status='failed', error='no_handler', "
                    "finished_at=now(), next_attempt_at=NULL "
                    "WHERE id=:id AND claim_token=:token"
                ),
                {"id": claimed.job_id, "token": str(claimed.claim_token)},
            )
            await session.commit()
        log.warning("no_handler", extra={"error": str(exc)})
    except Exception as exc:
        async with sm() as session:
            status = await record_failure(session, claimed=claimed, error=repr(exc))
            await session.commit()
        log.warning(
            "stage_error",
            extra={
                "error": repr(exc),
                "next_status": status,
                "max_attempts": MAX_ATTEMPTS,
            },
        )
    return True


__all__ = [
    "ClaimedJob",
    "claim_one",
    "mark_done_and_enqueue_downstream",
    "record_failure",
    "run_one_job",
    "unpark_claim",
]
