import hashlib
import json
import logging
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.snapshot  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pliny.api import deps
from pliny.db.models import Item
from pliny.db.queries import insert_item
from pliny.logging import get_logger
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import get_handler


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, item_entities, entities, item_tags, tags, items "
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


@pytest.fixture
def logger() -> logging.LoggerAdapter[logging.Logger]:
    return get_logger("test")


async def _seed(db_session: AsyncSession, *, canonical_url: str) -> uuid.UUID:
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
    snapshotter: object,
    logger: logging.LoggerAdapter[logging.Logger],
) -> StageContext:
    return StageContext(
        item_id=item_id,
        stage="snapshot",
        attempt=1,
        claim_token=uuid.uuid4(),
        db=db,
        blob=deps.get_blob(),
        llm=None,
        logger=logger,
        snapshotter=snapshotter,
    )


@respx.mock
async def test_snapshot_audio_mutates_type_and_writes_metadata(
    db_session: AsyncSession,
    fake_snapshotter,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    canonical = "https://cdn.example.com/track.mp3"
    item_id = await _seed(db_session, canonical_url=canonical)

    respx.head(canonical).mock(
        return_value=httpx.Response(200, headers={"content-type": "audio/mpeg"})
    )

    ctx = _ctx(item_id, db=db_session, snapshotter=fake_snapshotter, logger=logger)
    await get_handler("snapshot")(ctx)
    await db_session.commit()

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.type == "audio"
    assert item.raw_ref is not None and item.raw_ref.startswith("raw/")

    blob = deps.get_blob()
    raw = json.loads(await blob.get(item.raw_ref))
    assert raw["content_type"] == "audio/mpeg"
    assert raw["final_url"] == canonical

    assert not await blob.exists(f"derived/{item_id}/screenshot.png")
    assert await blob.exists(f"derived/{item_id}/metadata.json")
    assert fake_snapshotter.calls == []


@respx.mock
async def test_snapshot_video_mutates_type(
    db_session: AsyncSession,
    fake_snapshotter,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    canonical = "https://cdn.example.com/video.mp4"
    item_id = await _seed(db_session, canonical_url=canonical)

    respx.head(canonical).mock(
        return_value=httpx.Response(200, headers={"content-type": "video/mp4"})
    )

    ctx = _ctx(item_id, db=db_session, snapshotter=fake_snapshotter, logger=logger)
    await get_handler("snapshot")(ctx)
    await db_session.commit()

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.type == "video"
