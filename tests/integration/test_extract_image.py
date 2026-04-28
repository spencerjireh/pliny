import hashlib
import io
import logging
import uuid

import pytest
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.extract  # noqa: F401
from pliny.api import deps
from pliny.db.models import Content, ImagePhash, Item
from pliny.db.queries import insert_item
from pliny.logging import get_logger
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import get_handler


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


def _png(rgb: tuple[int, int, int], size: tuple[int, int] = (32, 32)) -> bytes:
    img = Image.new("RGB", size, rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _gradient_png(seed: int) -> bytes:
    """Two near-duplicate-but-not-identical images for pHash testing."""
    img = Image.new("RGB", (32, 32), (255, 255, 255))
    img.putpixel((0, 0), (250 - seed, 250 - seed, 250 - seed))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _ingest_image(
    db_session: AsyncSession, raw: bytes, *, mime: str = "image/png"
) -> uuid.UUID:
    item = await insert_item(
        db_session,
        type="image",
        content_hash=hashlib.sha256(raw).hexdigest(),
        raw_ref=f"raw/{hashlib.sha256(raw).hexdigest()}",
        metadata={"mime": mime},
    )
    blob = deps.get_blob()
    await blob.put(item.raw_ref, raw)  # type: ignore[arg-type]
    await db_session.commit()
    return item.id


async def _run_extract(
    item_id: uuid.UUID,
    fake_llm: object,
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    sm = deps.get_session_maker()
    async with sm() as session:
        ctx = StageContext(
            item_id=item_id,
            stage="extract",
            attempt=1,
            claim_token=uuid.uuid4(),
            db=session,
            blob=deps.get_blob(),
            llm=fake_llm,
            logger=logger,
        )
        await get_handler("extract")(ctx)
        await session.commit()


async def test_extract_image_writes_content_and_phash(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    raw = _png((255, 0, 0))
    item_id = await _ingest_image(db_session, raw)
    await _run_extract(item_id, fake_llm, logger)

    content = (
        await db_session.execute(select(Content).where(Content.item_id == item_id))
    ).scalar_one()
    assert content.extracted_text == fake_llm.vision_response_text
    assert content.extraction_method.startswith("vision:")

    ph = (
        await db_session.execute(select(ImagePhash).where(ImagePhash.item_id == item_id))
    ).scalar_one()
    assert isinstance(ph.phash, int)


async def test_phash_dup_sets_metadata(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    # Two near-duplicate images with different bytes (so dedup at ingest doesn't fire)
    raw_a = _gradient_png(seed=0)
    raw_b = _gradient_png(seed=1)
    assert raw_a != raw_b

    id_a = await _ingest_image(db_session, raw_a)
    await _run_extract(id_a, fake_llm, logger)
    id_b = await _ingest_image(db_session, raw_b)
    await _run_extract(id_b, fake_llm, logger)

    refreshed = (await db_session.execute(select(Item).where(Item.id == id_b))).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.meta is not None
    assert refreshed.meta.get("possible_duplicate_of") == str(id_a)


async def test_extract_image_requires_llm(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    await _truncate(db_session)
    raw = _png((0, 255, 0))
    item_id = await _ingest_image(db_session, raw)
    sm = deps.get_session_maker()
    async with sm() as session:
        ctx = StageContext(
            item_id=item_id,
            stage="extract",
            attempt=1,
            claim_token=uuid.uuid4(),
            db=session,
            blob=deps.get_blob(),
            llm=None,
            logger=logger,
        )
        with pytest.raises(RuntimeError):
            await get_handler("extract")(ctx)
