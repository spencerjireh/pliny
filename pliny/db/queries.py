import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.db.models import Item, ItemSource, ProcessingJob


async def find_item_by_content_hash(session: AsyncSession, content_hash: str) -> Item | None:
    stmt = select(Item).where(Item.content_hash == content_hash)
    return (await session.execute(stmt)).scalar_one_or_none()


async def insert_item(
    session: AsyncSession,
    *,
    type: str,
    content_hash: str,
    canonical_url: str | None = None,
    raw_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Item:
    """Insert a new item. On content_hash conflict, returns the existing row.

    The ORM attribute is `meta` because `metadata` is reserved on DeclarativeBase;
    the underlying SQL column is still named `metadata` per spec.
    """
    stmt = (
        pg_insert(Item)
        .values(
            id=uuid.uuid4(),
            type=type,
            content_hash=content_hash,
            canonical_url=canonical_url,
            raw_ref=raw_ref,
            meta=metadata,
        )
        .on_conflict_do_nothing(index_elements=["content_hash"])
    )
    await session.execute(stmt)
    existing = await find_item_by_content_hash(session, content_hash)
    assert existing is not None
    return existing


async def append_item_source(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    source: str,
    source_ref: str | None,
) -> bool:
    """Idempotent on (source, source_ref) when source_ref is not NULL."""
    stmt = pg_insert(ItemSource).values(
        id=uuid.uuid4(),
        item_id=item_id,
        source=source,
        source_ref=source_ref,
    )
    if source_ref is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["item_id", "source", "source_ref"],
            index_where=text("source_ref IS NOT NULL"),
        )
    stmt = stmt.returning(ItemSource.id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def enqueue_job(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    stage: str,
    pool: str,
) -> bool:
    """Insert a pending job; idempotent on (item_id, stage)."""
    stmt = (
        pg_insert(ProcessingJob)
        .values(
            id=uuid.uuid4(),
            item_id=item_id,
            stage=stage,
            pool=pool,
            status="pending",
            attempts=0,
            next_attempt_at=text("now()"),
        )
        .on_conflict_do_nothing(index_elements=["item_id", "stage"])
        .returning(ProcessingJob.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def notify(session: AsyncSession, channel: str, payload: str = "") -> None:
    """LISTEN/NOTIFY signal to wake worker pools."""
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)").bindparams(channel=channel, payload=payload)
    )
