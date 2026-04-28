import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.graph_sync  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pliny.api import deps
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


async def _seed_item_with_entities(
    db_session: AsyncSession,
    *,
    title: str,
    entity_specs: list[tuple[str, str, float | None]],
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    item = await insert_item(
        db_session,
        type="text",
        content_hash=uuid.uuid4().hex,
    )
    await db_session.execute(
        text("UPDATE items SET title = :t WHERE id = :id"),
        {"t": title, "id": item.id},
    )

    entity_ids: list[uuid.UUID] = []
    for name, etype, confidence in entity_specs:
        entity_id = uuid.uuid4()
        await db_session.execute(
            text("INSERT INTO entities (id, canonical_name, type) VALUES (:id, :n, :t)"),
            {"id": entity_id, "n": name, "t": etype},
        )
        await db_session.execute(
            text(
                "INSERT INTO item_entities "
                "(item_id, entity_id, mention_text, confidence, entities_version) "
                "VALUES (:i, :e, :m, :c, 1)"
            ),
            {"i": item.id, "e": entity_id, "m": name, "c": confidence},
        )
        entity_ids.append(entity_id)
    await db_session.commit()
    return item.id, entity_ids


async def _run_handler(
    item_id: uuid.UUID,
    driver: Any,
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    sm = deps.get_session_maker()
    async with sm() as session:
        ctx = StageContext(
            item_id=item_id,
            stage="graph_sync",
            attempt=1,
            claim_token=uuid.uuid4(),
            db=session,
            blob=deps.get_blob(),
            llm=None,
            logger=logger,
            neo4j=driver,
        )
        await get_handler("graph_sync")(ctx)
        await session.commit()


async def _count(driver: Any, cypher: str, **params: Any) -> int:
    async with driver.session() as s:
        result = await s.run(cypher, **params)
        record = await result.single()
        assert record is not None
        return int(record["c"])


async def test_graph_sync_writes_item_entity_and_mentions(
    db_session: AsyncSession,
    neo4j_driver: Any,
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id, _ = await _seed_item_with_entities(
        db_session,
        title="Article title",
        entity_specs=[("alice", "person", 0.9), ("acme", "org", 0.8)],
    )

    await _run_handler(item_id, neo4j_driver, logger)

    assert await _count(neo4j_driver, "MATCH (i:Item) RETURN count(i) AS c") == 1
    assert await _count(neo4j_driver, "MATCH (e:Entity) RETURN count(e) AS c") == 2
    assert (
        await _count(
            neo4j_driver,
            "MATCH (i:Item {id:$id})-[r:MENTIONS]->() RETURN count(r) AS c",
            id=str(item_id),
        )
        == 2
    )


async def test_graph_sync_idempotent(
    db_session: AsyncSession,
    neo4j_driver: Any,
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id, _ = await _seed_item_with_entities(
        db_session,
        title="T",
        entity_specs=[("alice", "person", 0.9)],
    )

    await _run_handler(item_id, neo4j_driver, logger)
    await _run_handler(item_id, neo4j_driver, logger)

    assert await _count(neo4j_driver, "MATCH (i:Item) RETURN count(i) AS c") == 1
    assert await _count(neo4j_driver, "MATCH (e:Entity) RETURN count(e) AS c") == 1
    assert (
        await _count(
            neo4j_driver,
            "MATCH ()-[r:MENTIONS]->() RETURN count(r) AS c",
        )
        == 1
    )


async def test_graph_sync_cleans_stale_mentions(
    db_session: AsyncSession,
    neo4j_driver: Any,
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id, entity_ids = await _seed_item_with_entities(
        db_session,
        title="T",
        entity_specs=[("alice", "person", 0.9), ("bob", "person", 0.9)],
    )
    await _run_handler(item_id, neo4j_driver, logger)
    assert (
        await _count(
            neo4j_driver,
            "MATCH (i:Item {id:$id})-[r:MENTIONS]->() RETURN count(r) AS c",
            id=str(item_id),
        )
        == 2
    )

    # Drop bob from item_entities; rerun.
    await db_session.execute(
        text("DELETE FROM item_entities WHERE item_id = :i AND entity_id = :e"),
        {"i": item_id, "e": entity_ids[1]},
    )
    await db_session.commit()

    await _run_handler(item_id, neo4j_driver, logger)
    assert (
        await _count(
            neo4j_driver,
            "MATCH (i:Item {id:$id})-[r:MENTIONS]->() RETURN count(r) AS c",
            id=str(item_id),
        )
        == 1
    )


async def test_graph_sync_empty_mentions_still_creates_item(
    db_session: AsyncSession,
    neo4j_driver: Any,
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id, _ = await _seed_item_with_entities(db_session, title="Lonely", entity_specs=[])
    await _run_handler(item_id, neo4j_driver, logger)

    assert (
        await _count(
            neo4j_driver,
            "MATCH (i:Item {id:$id}) RETURN count(i) AS c",
            id=str(item_id),
        )
        == 1
    )
    assert (
        await _count(
            neo4j_driver,
            "MATCH ()-[r:MENTIONS]->() RETURN count(r) AS c",
        )
        == 0
    )
