import logging
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.extract  # noqa: F401
from pliny.api import deps
from pliny.db.models import Content, Item
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


@respx.mock
async def test_extract_url_fetches_and_extracts(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    await _truncate(db_session)
    canonical = "https://example.com/article"
    item = await insert_item(
        db_session,
        type="url",
        content_hash=uuid.uuid4().hex,
        canonical_url=canonical,
    )
    await db_session.commit()

    respx.get(canonical).mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML, headers={"content-type": "text/html"})
    )

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
        await get_handler("extract")(ctx)
        await session.commit()

    content = (
        await db_session.execute(select(Content).where(Content.item_id == item.id))
    ).scalar_one()
    assert content.extracted_text is not None
    assert "body of the article" in content.extracted_text
    assert content.extraction_method == "trafilatura"
    assert content.extract_version == STAGE_VERSIONS["extract"]

    refreshed = (await db_session.execute(select(Item).where(Item.id == item.id))).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.raw_ref is not None
    assert refreshed.raw_ref.startswith("raw/")


@respx.mock
async def test_extract_url_failed_response_raises(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    await _truncate(db_session)
    canonical = "https://example.com/dead"
    item = await insert_item(
        db_session,
        type="url",
        content_hash=uuid.uuid4().hex,
        canonical_url=canonical,
    )
    await db_session.commit()

    respx.get(canonical).mock(return_value=httpx.Response(500))

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
        with pytest.raises(httpx.HTTPStatusError):
            await get_handler("extract")(ctx)
