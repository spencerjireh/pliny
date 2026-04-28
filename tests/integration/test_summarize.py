import json
import logging
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.summarize  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pliny.api import deps
from pliny.db.models import Item, ItemTag, Tag
from pliny.db.queries import insert_item
from pliny.logging import get_logger
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import get_handler


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, item_tags, tags, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


@pytest.fixture
def logger() -> logging.LoggerAdapter[logging.Logger]:
    return get_logger("test")


async def _seed_with_text(db_session: AsyncSession, *, extracted_text: str) -> uuid.UUID:
    item = await insert_item(
        db_session,
        type="text",
        content_hash=uuid.uuid4().hex,
        raw_ref=None,
    )
    if extracted_text:
        await db_session.execute(
            text(
                "INSERT INTO content (item_id, extracted_text, extraction_method, extract_version) "
                "VALUES (:id, :t, 'identity', 1)"
            ),
            {"id": item.id, "t": extracted_text},
        )
    await db_session.commit()
    return item.id


async def _run_handler(
    item_id: uuid.UUID, llm: object, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    sm = deps.get_session_maker()
    async with sm() as session:
        ctx = StageContext(
            item_id=item_id,
            stage="summarize",
            attempt=1,
            claim_token=uuid.uuid4(),
            db=session,
            blob=deps.get_blob(),
            llm=llm,
            logger=logger,
        )
        await get_handler("summarize")(ctx)
        await session.commit()


async def test_summarize_writes_title_summary_and_tags(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    fake_llm.chat_response_text = json.dumps(
        {"title": "Pliny Article", "summary": "An article about Pliny.", "tags": ["pliny", "test"]}
    )
    item_id = await _seed_with_text(db_session, extracted_text="An article about Pliny.")
    await _run_handler(item_id, fake_llm, logger)

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.title == "Pliny Article"
    assert item.summary == "An article about Pliny."

    tag_names = (
        (
            await db_session.execute(
                select(Tag.name)
                .join(ItemTag, Tag.id == ItemTag.tag_id)
                .where(ItemTag.item_id == item_id)
            )
        )
        .scalars()
        .all()
    )
    assert sorted(tag_names) == ["pliny", "test"]
    assert len(fake_llm.chat_calls) == 1


async def test_summarize_empty_content_no_llm_call(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id = await _seed_with_text(db_session, extracted_text="")
    # Insert content row with NULL extracted_text
    await db_session.execute(
        text(
            "INSERT INTO content (item_id, extracted_text, extraction_method, extract_version) "
            "VALUES (:id, NULL, 'identity', 1)"
        ),
        {"id": item_id},
    )
    await db_session.commit()

    await _run_handler(item_id, fake_llm, logger)

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.title is None
    assert item.summary is None
    assert fake_llm.chat_calls == []


async def test_summarize_idempotent_replaces_tags(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id = await _seed_with_text(db_session, extracted_text="Body text here.")

    fake_llm.chat_response_text = json.dumps(
        {"title": "T1", "summary": "S1.", "tags": ["alpha", "beta"]}
    )
    await _run_handler(item_id, fake_llm, logger)

    fake_llm.chat_response_text = json.dumps({"title": "T2", "summary": "S2.", "tags": ["gamma"]})
    await _run_handler(item_id, fake_llm, logger)

    tag_names = (
        (
            await db_session.execute(
                select(Tag.name)
                .join(ItemTag, Tag.id == ItemTag.tag_id)
                .where(ItemTag.item_id == item_id)
            )
        )
        .scalars()
        .all()
    )
    assert tag_names == ["gamma"]


async def test_summarize_malformed_json_raises(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    fake_llm.chat_response_text = "not json at all"
    item_id = await _seed_with_text(db_session, extracted_text="Body.")
    with pytest.raises(json.JSONDecodeError):
        await _run_handler(item_id, fake_llm, logger)
