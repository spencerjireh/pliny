import uuid
from typing import TYPE_CHECKING, Any, TypedDict

from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.db.models import (
    Chunk,
    Entity,
    Item,
    ItemEntity,
    ItemSource,
    ItemTag,
    ProcessingJob,
    Tag,
)

if TYPE_CHECKING:
    from pliny.pipeline.chunk.chunker import ChunkPiece


class EntityMention(TypedDict):
    name: str
    type: str
    mention_text: str | None
    confidence: float | None
    aliases: list[str] | None


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


async def replace_chunks(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    pieces: "list[ChunkPiece]",
    version: int,
) -> None:
    """Delete-and-replace all chunks for an item (cascades to embeddings)."""
    await session.execute(
        text("DELETE FROM chunks WHERE item_id = :id"),
        {"id": item_id},
    )
    if not pieces:
        return
    await session.execute(
        insert(Chunk),
        [
            {
                "id": uuid.uuid4(),
                "item_id": item_id,
                "chunk_index": p.index,
                "text": p.text,
                "token_count": p.token_count,
                "chunker_version": version,
            }
            for p in pieces
        ],
    )


async def upsert_tag(session: AsyncSession, *, name: str) -> uuid.UUID:
    """Insert tag if missing; return its id."""
    stmt = (
        pg_insert(Tag)
        .values(id=uuid.uuid4(), name=name)
        .on_conflict_do_update(index_elements=["name"], set_={"name": name})
        .returning(Tag.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def link_item_tag(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    stmt = pg_insert(ItemTag).values(item_id=item_id, tag_id=tag_id).on_conflict_do_nothing()
    await session.execute(stmt)


async def replace_item_entities(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    mentions: list[EntityMention],
    version: int,
) -> None:
    """Delete-and-replace `item_entities` rows for an item.

    Pre-lowercases canonical_name so the (canonical_name, type) unique constraint
    acts as case-insensitive matching. Reuses an existing entity row when one
    exists; otherwise inserts a new one. Aliases are stored on first creation
    only (we don't merge alias lists in v1).
    """
    await session.execute(
        text("DELETE FROM item_entities WHERE item_id = :id"),
        {"id": item_id},
    )
    if not mentions:
        return

    rows: dict[uuid.UUID, dict[str, Any]] = {}
    for m in mentions:
        canonical = m["name"].strip().lower()
        if not canonical:
            continue
        stmt = (
            pg_insert(Entity)
            .values(
                id=uuid.uuid4(),
                canonical_name=canonical,
                type=m["type"],
                aliases=m.get("aliases"),
            )
            .on_conflict_do_update(
                index_elements=["canonical_name", "type"],
                set_={"canonical_name": canonical},
            )
            .returning(Entity.id)
        )
        entity_id = (await session.execute(stmt)).scalar_one()
        rows.setdefault(
            entity_id,
            {
                "item_id": item_id,
                "entity_id": entity_id,
                "mention_text": m.get("mention_text"),
                "confidence": m.get("confidence"),
                "entities_version": version,
            },
        )

    if rows:
        await session.execute(insert(ItemEntity), list(rows.values()))
