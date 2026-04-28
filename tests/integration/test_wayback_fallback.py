import logging
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.wayback_fallback  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pliny.api import deps
from pliny.db.models import Item
from pliny.db.queries import insert_item
from pliny.logging import get_logger
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import get_handler
from pliny.pipeline.wayback_fallback.handler import WaybackUnavailable

WAYBACK_API = "https://archive.org/wayback/available"


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


async def _seed(db_session: AsyncSession, *, canonical_url: str) -> uuid.UUID:
    import hashlib

    item = await insert_item(
        db_session,
        type="url",
        content_hash=hashlib.sha256(canonical_url.encode()).hexdigest(),
        canonical_url=canonical_url,
    )
    await db_session.commit()
    return item.id


def _ctx(
    item_id: uuid.UUID,
    *,
    db: AsyncSession,
    logger: logging.LoggerAdapter[logging.Logger],
) -> StageContext:
    return StageContext(
        item_id=item_id,
        stage="wayback_fallback",
        attempt=1,
        claim_token=uuid.uuid4(),
        db=db,
        blob=deps.get_blob(),
        llm=None,
        logger=logger,
    )


@respx.mock
async def test_wayback_fetches_archived_html(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    await _truncate(db_session)
    canonical = "https://dead.example.com/article"
    item_id = await _seed(db_session, canonical_url=canonical)

    archive_url = "https://web.archive.org/web/20240101000000/" + canonical
    respx.get(WAYBACK_API).mock(
        return_value=httpx.Response(
            200,
            json={
                "archived_snapshots": {
                    "closest": {
                        "status": "200",
                        "timestamp": "20240101000000",
                        "url": archive_url,
                    }
                }
            },
        )
    )
    archived_html = b"<html>archived</html>"
    respx.get(archive_url).mock(
        return_value=httpx.Response(
            200, content=archived_html, headers={"content-type": "text/html"}
        )
    )

    ctx = _ctx(item_id, db=db_session, logger=logger)
    await get_handler("wayback_fallback")(ctx)
    await db_session.commit()

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.raw_ref is not None
    assert (await deps.get_blob().get(item.raw_ref)) == archived_html
    assert item.meta is not None
    assert item.meta["archive_source"] == "wayback"
    assert item.meta["archive_timestamp"] == "20240101000000"


@respx.mock
async def test_wayback_no_snapshot_raises(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    await _truncate(db_session)
    canonical = "https://nothing.example.com/x"
    item_id = await _seed(db_session, canonical_url=canonical)

    respx.get(WAYBACK_API).mock(return_value=httpx.Response(200, json={"archived_snapshots": {}}))

    ctx = _ctx(item_id, db=db_session, logger=logger)
    with pytest.raises(WaybackUnavailable):
        await get_handler("wayback_fallback")(ctx)


@respx.mock
async def test_wayback_non_200_archived_status_raises(
    db_session: AsyncSession, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    await _truncate(db_session)
    canonical = "https://flaky.example.com/x"
    item_id = await _seed(db_session, canonical_url=canonical)

    respx.get(WAYBACK_API).mock(
        return_value=httpx.Response(
            200,
            json={
                "archived_snapshots": {
                    "closest": {
                        "status": "404",
                        "timestamp": "20240101",
                        "url": "https://web.archive.org/x",
                    }
                }
            },
        )
    )
    ctx = _ctx(item_id, db=db_session, logger=logger)
    with pytest.raises(WaybackUnavailable):
        await get_handler("wayback_fallback")(ctx)
