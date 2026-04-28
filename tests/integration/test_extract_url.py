import logging
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.extract  # noqa: F401
from pliny.api import deps
from pliny.db.models import Content
from pliny.db.queries import insert_item
from pliny.logging import get_logger
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import STAGE_VERSIONS, get_handler

SAMPLE_HTML = """<!DOCTYPE html>
<html lang="en"><head><title>Hello</title></head>
<body>
<h1>The Title</h1>
<p>This is the body of the article. It has enough text for trafilatura to be
happy with extraction. Here is a second sentence to make it more substantial.</p>
</body></html>
"""


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


async def test_extract_url_reads_raw_ref(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    """After snapshot pre-populates raw_ref, extract reads the blob and runs
    trafilatura on the stored bytes — no network fetch."""
    await _truncate(db_session)
    canonical = "https://example.com/article"

    blob = deps.get_blob()
    raw_ref = "raw/abc123"
    await blob.put(raw_ref, SAMPLE_HTML.encode("utf-8"))

    item = await insert_item(
        db_session,
        type="url",
        content_hash=uuid.uuid4().hex,
        canonical_url=canonical,
        raw_ref=raw_ref,
    )
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
    assert content.extracted_text is not None
    assert "body of the article" in content.extracted_text
    assert content.extraction_method == "trafilatura"
    assert content.extract_version == STAGE_VERSIONS["extract"]


async def test_extract_url_without_raw_ref_raises(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    """A URL item that reaches extract before snapshot ran is a programming
    error — we surface it loudly rather than silently fetching the network."""
    await _truncate(db_session)
    item = await insert_item(
        db_session,
        type="url",
        content_hash=uuid.uuid4().hex,
        canonical_url="https://example.com/never-snapshotted",
    )
    await db_session.commit()

    sm = deps.get_session_maker()
    async with sm() as session:
        ctx = StageContext(
            item_id=item.id,
            stage="extract",
            attempt=1,
            claim_token=uuid.uuid4(),
            db=session,
            blob=deps.get_blob(),
            llm=None,
            logger=logger,
        )
        with pytest.raises(RuntimeError, match="raw_ref"):
            await get_handler("extract")(ctx)
