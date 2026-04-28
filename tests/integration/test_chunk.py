import logging
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.chunk  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pliny.api import deps
from pliny.db.models import Chunk, Item
from pliny.db.queries import insert_item
from pliny.logging import get_logger
from pliny.pipeline.chunk import chunker
from pliny.pipeline.chunk.chunker import ENCODING
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import get_handler
from pliny.storage.blob import BlobStore


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


@pytest.fixture
def logger() -> logging.LoggerAdapter[logging.Logger]:
    return get_logger("test")


async def _seed_item_with_text(db_session: AsyncSession, *, extracted_text: str) -> uuid.UUID:
    item = await insert_item(
        db_session,
        type="text",
        content_hash=uuid.uuid4().hex,
        raw_ref=None,
    )
    await db_session.execute(
        text(
            "INSERT INTO content (item_id, extracted_text, extraction_method, extract_version) "
            "VALUES (:id, :t, 'identity', 1)"
        ),
        {"id": item.id, "t": extracted_text},
    )
    await db_session.commit()
    return item.id


async def _run_chunk_handler(
    item_id: uuid.UUID,
    blob: BlobStore,
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    sm = deps.get_session_maker()
    async with sm() as session:
        ctx = StageContext(
            item_id=item_id,
            stage="chunk",
            attempt=1,
            claim_token=uuid.uuid4(),
            db=session,
            blob=blob,
            llm=None,
            logger=logger,
        )
        await get_handler("chunk")(ctx)
        await session.commit()


async def test_chunk_writes_rows_in_order(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    await _truncate(db_session)
    word = "alpha "
    tokens = ENCODING.encode(word * 4000)[:2000]
    extracted = ENCODING.decode(tokens)
    item_id = await _seed_item_with_text(db_session, extracted_text=extracted)

    await _run_chunk_handler(item_id, deps.get_blob(), logger)

    chunks = (
        (
            await db_session.execute(
                select(Chunk).where(Chunk.item_id == item_id).order_by(Chunk.chunk_index)
            )
        )
        .scalars()
        .all()
    )
    assert len(chunks) > 1
    for i, c in enumerate(chunks):
        assert c.chunk_index == i
        assert c.token_count > 0
        assert c.chunker_version == 1


async def test_chunk_idempotent(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    await _truncate(db_session)
    extracted = "hello world " * 200
    item_id = await _seed_item_with_text(db_session, extracted_text=extracted)

    await _run_chunk_handler(item_id, deps.get_blob(), logger)
    first = (
        (await db_session.execute(select(Chunk).where(Chunk.item_id == item_id))).scalars().all()
    )
    await _run_chunk_handler(item_id, deps.get_blob(), logger)
    second = (
        (await db_session.execute(select(Chunk).where(Chunk.item_id == item_id))).scalars().all()
    )

    assert len(first) == len(second)


async def test_chunk_overflow_metadata(
    db_session: AsyncSession,
    logger: logging.LoggerAdapter[logging.Logger],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _truncate(db_session)
    monkeypatch.setattr(chunker, "MAX_CHUNKS", 3)
    extracted = ENCODING.decode(list(range(1000, 3000)))
    item_id = await _seed_item_with_text(db_session, extracted_text=extracted)

    await _run_chunk_handler(item_id, deps.get_blob(), logger)

    chunks = (
        (await db_session.execute(select(Chunk).where(Chunk.item_id == item_id))).scalars().all()
    )
    assert len(chunks) == 3

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.meta is not None
    assert item.meta.get("chunk_overflow") is True
    assert item.meta.get("original_chunk_count", 0) > 3


async def test_chunk_empty_text_writes_nothing(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    await _truncate(db_session)
    item_id = await _seed_item_with_text(db_session, extracted_text="")

    await _run_chunk_handler(item_id, deps.get_blob(), logger)

    chunks = (
        (await db_session.execute(select(Chunk).where(Chunk.item_id == item_id))).scalars().all()
    )
    assert chunks == []
