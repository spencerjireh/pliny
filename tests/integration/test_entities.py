import json
import logging
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.entities  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pliny.api import deps
from pliny.db.models import Entity, ItemEntity
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


async def _seed_item(
    db_session: AsyncSession,
    *,
    extracted_text: str | None,
    summary: str | None = None,
) -> uuid.UUID:
    item = await insert_item(
        db_session,
        type="text",
        content_hash=uuid.uuid4().hex,
        raw_ref=None,
    )
    if summary is not None:
        await db_session.execute(
            text("UPDATE items SET summary = :s WHERE id = :id"),
            {"s": summary, "id": item.id},
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


async def _run_handler(
    item_id: uuid.UUID, llm: object, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    sm = deps.get_session_maker()
    async with sm() as session:
        ctx = StageContext(
            item_id=item_id,
            stage="entities",
            attempt=1,
            claim_token=uuid.uuid4(),
            db=session,
            blob=deps.get_blob(),
            llm=llm,
            logger=logger,
        )
        await get_handler("entities")(ctx)
        await session.commit()


async def test_entities_writes_entity_and_link_rows(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    fake_llm.chat_response_text = json.dumps(
        {
            "entities": [
                {
                    "name": "Albert Einstein",
                    "type": "person",
                    "mention_text": "Einstein",
                    "confidence": 0.95,
                    "aliases": ["Einstein"],
                },
                {
                    "name": "University of Bern",
                    "type": "org",
                    "mention_text": "Bern",
                    "confidence": 0.8,
                },
                {
                    "name": "Relativity",
                    "type": "concept",
                    "mention_text": "relativity",
                    "confidence": 0.7,
                },
            ]
        }
    )
    item_id = await _seed_item(
        db_session,
        extracted_text="Einstein worked at the University of Bern on relativity.",
        summary="A short summary.",
    )
    await _run_handler(item_id, fake_llm, logger)

    entities = (await db_session.execute(select(Entity))).scalars().all()
    canonical_names = sorted(e.canonical_name for e in entities)
    assert canonical_names == ["albert einstein", "relativity", "university of bern"]

    links = (
        (await db_session.execute(select(ItemEntity).where(ItemEntity.item_id == item_id)))
        .scalars()
        .all()
    )
    assert len(links) == 3
    assert all(link.entities_version == 1 for link in links)
    assert {link.mention_text for link in links} == {"Einstein", "Bern", "relativity"}


async def test_entities_empty_content_no_llm_call(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id = await _seed_item(db_session, extracted_text=None)
    await _run_handler(item_id, fake_llm, logger)

    assert fake_llm.chat_calls == []
    links = (
        (await db_session.execute(select(ItemEntity).where(ItemEntity.item_id == item_id)))
        .scalars()
        .all()
    )
    assert links == []


async def test_entities_idempotent_replaces_links(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id = await _seed_item(db_session, extracted_text="Body text.")

    fake_llm.chat_response_text = json.dumps(
        {
            "entities": [
                {"name": "Alpha", "type": "concept", "mention_text": "alpha", "confidence": 0.9},
                {"name": "Beta", "type": "concept", "mention_text": "beta", "confidence": 0.9},
            ]
        }
    )
    await _run_handler(item_id, fake_llm, logger)

    fake_llm.chat_response_text = json.dumps(
        {"entities": [{"name": "Gamma", "type": "concept", "confidence": 0.9}]}
    )
    await _run_handler(item_id, fake_llm, logger)

    link_names = sorted(
        (
            await db_session.execute(
                select(Entity.canonical_name)
                .join(ItemEntity, Entity.id == ItemEntity.entity_id)
                .where(ItemEntity.item_id == item_id)
            )
        )
        .scalars()
        .all()
    )
    assert link_names == ["gamma"]

    # alpha+beta entity rows survive (might be referenced by other items in future)
    all_entity_names = sorted(
        (await db_session.execute(select(Entity.canonical_name))).scalars().all()
    )
    assert all_entity_names == ["alpha", "beta", "gamma"]


async def test_entities_cross_item_dedup(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_a = await _seed_item(db_session, extracted_text="Apple released a product.")
    item_b = await _seed_item(db_session, extracted_text="Banana growers met.")

    fake_llm.chat_response_text = json.dumps(
        {"entities": [{"name": "Apple", "type": "org", "confidence": 0.9}]}
    )
    await _run_handler(item_a, fake_llm, logger)

    fake_llm.chat_response_text = json.dumps(
        {
            "entities": [
                {"name": "Apple", "type": "org", "confidence": 0.9},
                {"name": "Banana", "type": "concept", "confidence": 0.8},
            ]
        }
    )
    await _run_handler(item_b, fake_llm, logger)

    all_entity_names = sorted(
        (await db_session.execute(select(Entity.canonical_name))).scalars().all()
    )
    assert all_entity_names == ["apple", "banana"]

    a_links = (
        (await db_session.execute(select(ItemEntity).where(ItemEntity.item_id == item_a)))
        .scalars()
        .all()
    )
    b_links = (
        (await db_session.execute(select(ItemEntity).where(ItemEntity.item_id == item_b)))
        .scalars()
        .all()
    )
    assert len(a_links) == 1
    assert len(b_links) == 2


async def test_entities_invalid_type_filtered(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id = await _seed_item(db_session, extracted_text="Body.")
    fake_llm.chat_response_text = json.dumps(
        {
            "entities": [
                {"name": "Valid", "type": "concept", "confidence": 0.9},
                {"name": "Invalid", "type": "alien", "confidence": 0.9},
                {"name": "", "type": "concept", "confidence": 0.9},
            ]
        }
    )
    await _run_handler(item_id, fake_llm, logger)

    names = sorted((await db_session.execute(select(Entity.canonical_name))).scalars().all())
    assert names == ["valid"]


async def test_entities_malformed_json_raises(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    fake_llm.chat_response_text = "not json"
    item_id = await _seed_item(db_session, extracted_text="Body.")
    with pytest.raises(json.JSONDecodeError):
        await _run_handler(item_id, fake_llm, logger)
