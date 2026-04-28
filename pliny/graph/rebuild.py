from typing import Any, LiteralString

from neo4j import AsyncDriver
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.graph.schema import ensure_constraints

WIPE = "MATCH (n) DETACH DELETE n"

INSERT_ITEMS = """
UNWIND $rows AS r
MERGE (i:Item {id: r.id})
SET i.title = r.title, i.captured_at = r.captured_at, i.type = r.type
"""

INSERT_ENTITIES = """
UNWIND $rows AS r
MERGE (e:Entity {id: r.id})
SET e.canonical_name = r.canonical_name, e.type = r.type
"""

INSERT_MENTIONS = """
UNWIND $rows AS r
MATCH (i:Item {id: r.item_id})
MATCH (e:Entity {id: r.entity_id})
MERGE (i)-[m:MENTIONS]->(e)
SET m.confidence = r.confidence
"""

INSERT_RELATED_TO = """
UNWIND $rows AS r
MATCH (a:Entity {id: r.from_id})
MATCH (b:Entity {id: r.to_id})
MERGE (a)-[rel:RELATED_TO {source: 'cooccurrence'}]->(b)
SET rel.weight = r.weight
"""

BATCH = 500


async def rebuild_from_postgres(driver: AsyncDriver, db: AsyncSession) -> dict[str, int]:
    """Drop and rewrite Neo4j from Postgres.

    Wipes all nodes/edges, recreates uniqueness constraints, then batch-inserts
    Items, Entities, MENTIONS, and co-occurrence RELATED_TO via UNWIND.
    Co-occurrence pairs are emitted in canonical (from_id < to_id) order so
    only one RELATED_TO exists between any two entities.

    Documented to be run serially; no advisory lock in v1.
    """
    items = (
        (await db.execute(sql_text("SELECT id, title, captured_at, type FROM items")))
        .mappings()
        .all()
    )
    entities = (
        (await db.execute(sql_text("SELECT id, canonical_name, type FROM entities")))
        .mappings()
        .all()
    )
    mentions = (
        (await db.execute(sql_text("SELECT item_id, entity_id, confidence FROM item_entities")))
        .mappings()
        .all()
    )
    cooccurrence = (
        (
            await db.execute(
                sql_text(
                    "SELECT a.entity_id AS from_id, b.entity_id AS to_id, "
                    "       count(*)::int AS weight "
                    "FROM item_entities a "
                    "JOIN item_entities b ON a.item_id = b.item_id "
                    "WHERE a.entity_id < b.entity_id "
                    "GROUP BY a.entity_id, b.entity_id"
                )
            )
        )
        .mappings()
        .all()
    )

    async with driver.session() as s:
        await s.run(WIPE)
    await ensure_constraints(driver)

    items_payload: list[dict[str, Any]] = [
        {
            "id": str(r["id"]),
            "title": r["title"] or "",
            "captured_at": r["captured_at"].isoformat() if r["captured_at"] else None,
            "type": r["type"],
        }
        for r in items
    ]
    entities_payload: list[dict[str, Any]] = [
        {"id": str(r["id"]), "canonical_name": r["canonical_name"], "type": r["type"]}
        for r in entities
    ]
    mentions_payload: list[dict[str, Any]] = [
        {
            "item_id": str(r["item_id"]),
            "entity_id": str(r["entity_id"]),
            "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
        }
        for r in mentions
    ]
    cooccurrence_payload: list[dict[str, Any]] = [
        {"from_id": str(r["from_id"]), "to_id": str(r["to_id"]), "weight": int(r["weight"])}
        for r in cooccurrence
    ]

    async def _batched(stmt: LiteralString, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        async with driver.session() as s:
            for i in range(0, len(rows), BATCH):
                await s.run(stmt, rows=rows[i : i + BATCH])

    await _batched(INSERT_ITEMS, items_payload)
    await _batched(INSERT_ENTITIES, entities_payload)
    await _batched(INSERT_MENTIONS, mentions_payload)
    await _batched(INSERT_RELATED_TO, cooccurrence_payload)

    return {
        "items": len(items_payload),
        "entities": len(entities_payload),
        "mentions": len(mentions_payload),
        "related_to": len(cooccurrence_payload),
    }
