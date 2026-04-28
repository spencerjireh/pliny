import logging
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.extract  # noqa: F401  -- registers handlers
from pliny.api import deps
from pliny.db.models import Content
from pliny.db.queries import insert_item
from pliny.logging import get_logger
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import STAGE_VERSIONS, get_handler


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


async def test_extract_text_writes_content_row(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    await _truncate(db_session)
    raw = b"hello pliny"
    blob = deps.get_blob()
    item = await insert_item(
        db_session,
        type="text",
        content_hash=uuid.uuid4().hex,
        raw_ref="raw/test-text",
    )
    await blob.put("raw/test-text", raw)
    await db_session.commit()

    sm = deps.get_session_maker()
    async with sm() as session:
        ctx = StageContext(
            item_id=item.id,
            stage="extract",
            attempt=1,
            claim_token=uuid.uuid4(),
            db=session,
            blob=blob,
            llm=None,
            logger=logger,
        )
        await get_handler("extract")(ctx)
        await session.commit()

    content = (
        await db_session.execute(select(Content).where(Content.item_id == item.id))
    ).scalar_one()
    assert content.extracted_text == "hello pliny"
    assert content.extraction_method == "identity"
    assert content.extract_version == STAGE_VERSIONS["extract"]
