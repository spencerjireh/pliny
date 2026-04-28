import hashlib
import logging
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.snapshot  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pliny.api import deps
from pliny.db.models import Item, ItemRedirect, ItemSource
from pliny.db.queries import append_item_source, insert_item
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


def _hash_url(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


async def _seed_url_item(
    db_session: AsyncSession,
    *,
    canonical_url: str,
    snapshot_done: bool = False,
    raw_ref: str | None = None,
) -> uuid.UUID:
    item = await insert_item(
        db_session,
        type="url",
        content_hash=_hash_url(canonical_url),
        canonical_url=canonical_url,
        raw_ref=raw_ref,
    )
    if snapshot_done:
        await db_session.execute(
            text("UPDATE items SET snapshot_version = 1 WHERE id = :id"),
            {"id": item.id},
        )
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
async def test_redirect_collision_into_existing_survivor(
    db_session: AsyncSession,
    fake_snapshotter,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)

    # Survivor: already exists, snapshot already done.
    canonical = "https://example.com/canonical"
    survivor_id = await _seed_url_item(
        db_session, canonical_url=canonical, snapshot_done=True, raw_ref="raw/existing"
    )
    await append_item_source(db_session, item_id=survivor_id, source="api", source_ref="alpha")

    # New item ingested via a different surface URL that resolves to canonical.
    short = "https://example.com/redir"
    new_id = await _seed_url_item(db_session, canonical_url=short)
    await append_item_source(db_session, item_id=new_id, source="api", source_ref="beta")
    await db_session.commit()

    respx.head(short).mock(return_value=httpx.Response(301, headers={"location": canonical}))
    respx.head(canonical).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"})
    )

    fake_snapshotter.final_url_override = canonical

    ctx = _ctx(new_id, db=db_session, snapshotter=fake_snapshotter, logger=logger)
    handler = get_handler("snapshot")
    await handler(ctx)
    assert ctx.skip_downstream is True
    await db_session.commit()

    # New item is gone; survivor still has its original raw_ref (snapshot_version was 1 already).
    new = (await db_session.execute(select(Item).where(Item.id == new_id))).scalar_one_or_none()
    assert new is None

    survivor = (await db_session.execute(select(Item).where(Item.id == survivor_id))).scalar_one()
    await db_session.refresh(survivor)
    assert survivor.raw_ref == "raw/existing"

    # Redirect row points new -> survivor.
    redirect = (
        await db_session.execute(select(ItemRedirect).where(ItemRedirect.from_id == new_id))
    ).scalar_one()
    assert redirect.to_id == survivor_id
    assert redirect.reason == "redirect_collision"

    # Sources transferred (survivor now has both "alpha" and "beta" refs).
    sources = (
        (await db_session.execute(select(ItemSource).where(ItemSource.item_id == survivor_id)))
        .scalars()
        .all()
    )
    refs = sorted(s.source_ref for s in sources)
    assert refs == ["alpha", "beta"]


@respx.mock
async def test_redirect_collision_when_survivor_has_no_snapshot(
    db_session: AsyncSession,
    fake_snapshotter,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    """Survivor existed but never got snapshotted; transfer the fresh artifacts."""
    await _truncate(db_session)

    canonical = "https://example.com/page"
    survivor_id = await _seed_url_item(db_session, canonical_url=canonical)
    short = "https://example.com/short"
    new_id = await _seed_url_item(db_session, canonical_url=short)
    await db_session.commit()

    respx.head(short).mock(return_value=httpx.Response(301, headers={"location": canonical}))
    respx.head(canonical).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"})
    )

    fake_snapshotter.final_url_override = canonical
    fake_snapshotter.rendered_html = b"<html>fresh</html>"

    ctx = _ctx(new_id, db=db_session, snapshotter=fake_snapshotter, logger=logger)
    handler = get_handler("snapshot")
    await handler(ctx)
    assert ctx.skip_downstream is True
    await db_session.commit()

    survivor = (await db_session.execute(select(Item).where(Item.id == survivor_id))).scalar_one()
    await db_session.refresh(survivor)
    assert survivor.raw_ref is not None
    assert survivor.raw_ref.startswith("raw/")
    assert survivor.snapshot_version == 1

    blob = deps.get_blob()
    raw = await blob.get(survivor.raw_ref)
    assert raw == b"<html>fresh</html>"
    assert await blob.exists(f"derived/{survivor_id}/screenshot.png")
    assert await blob.exists(f"derived/{survivor_id}/metadata.json")

    # Extract job enqueued on the survivor so its pipeline progresses.
    jobs = (
        (
            await db_session.execute(
                text("SELECT stage, pool, status FROM processing_jobs WHERE item_id = :id"),
                {"id": survivor_id},
            )
        )
        .mappings()
        .all()
    )
    assert any(j["stage"] == "extract" and j["pool"] == "fast" for j in jobs)
