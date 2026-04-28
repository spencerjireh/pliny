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


async def _seed_url_item(db_session: AsyncSession, *, canonical_url: str) -> uuid.UUID:
    import hashlib

    h = hashlib.sha256(canonical_url.encode()).hexdigest()
    item = await insert_item(
        db_session,
        type="url",
        content_hash=h,
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
async def test_snapshot_html_writes_raw_screenshot_metadata(
    db_session: AsyncSession,
    fake_snapshotter,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    canonical = "https://example.com/article"
    item_id = await _seed_url_item(db_session, canonical_url=canonical)

    respx.head(canonical).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"})
    )

    fake_snapshotter.rendered_html = b"<html><body><p>hi</p></body></html>"
    fake_snapshotter.page_title = "Example"

    ctx = _ctx(item_id, db=db_session, snapshotter=fake_snapshotter, logger=logger)
    handler = get_handler("snapshot")
    await handler(ctx)
    assert ctx.skip_downstream is False
    await db_session.commit()

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.raw_ref is not None
    assert item.raw_ref.startswith("raw/")

    blob = deps.get_blob()
    raw_bytes = await blob.get(item.raw_ref)
    assert raw_bytes == fake_snapshotter.rendered_html

    assert await blob.exists(f"derived/{item_id}/screenshot.png")
    assert await blob.exists(f"derived/{item_id}/metadata.json")
    metadata_bytes = await blob.get(f"derived/{item_id}/metadata.json")
    metadata = json.loads(metadata_bytes)
    assert metadata["final_url"] == canonical
    assert metadata["content_type"] == "text/html"
    assert metadata["page_title"] == "Example"
    assert "fetched_at" in metadata

    assert item.meta is not None
    assert item.meta["final_url"] == canonical
    assert item.meta["page_title"] == "Example"


@respx.mock
async def test_snapshot_html_recanonicalizes_after_redirect(
    db_session: AsyncSession,
    fake_snapshotter,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    short = "https://example.com/short"
    long = "https://example.com/long?utm_source=foo"  # canonicalize strips utm
    expected_canonical = "https://example.com/long"
    item_id = await _seed_url_item(db_session, canonical_url=short)

    respx.head(short).mock(return_value=httpx.Response(301, headers={"location": long}))
    respx.head(long).mock(return_value=httpx.Response(200, headers={"content-type": "text/html"}))

    fake_snapshotter.final_url_override = long

    ctx = _ctx(item_id, db=db_session, snapshotter=fake_snapshotter, logger=logger)
    handler = get_handler("snapshot")
    await handler(ctx)
    await db_session.commit()

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.canonical_url == expected_canonical


@respx.mock
async def test_snapshot_html_idempotent_when_run_twice(
    db_session: AsyncSession,
    fake_snapshotter,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    canonical = "https://example.com/x"
    item_id = await _seed_url_item(db_session, canonical_url=canonical)

    respx.head(canonical).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"})
    )

    handler = get_handler("snapshot")

    ctx = _ctx(item_id, db=db_session, snapshotter=fake_snapshotter, logger=logger)
    await handler(ctx)
    await db_session.commit()
    item1 = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item1)
    raw_ref_1 = item1.raw_ref

    ctx = _ctx(item_id, db=db_session, snapshotter=fake_snapshotter, logger=logger)
    await handler(ctx)
    await db_session.commit()
    item2 = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item2)
    assert item2.raw_ref == raw_ref_1


async def test_snapshot_rejects_non_url_item(
    db_session: AsyncSession,
    fake_snapshotter,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    await db_session.commit()

    ctx = _ctx(item.id, db=db_session, snapshotter=fake_snapshotter, logger=logger)
    handler = get_handler("snapshot")
    with pytest.raises(RuntimeError, match="expected 'url'"):
        await handler(ctx)
