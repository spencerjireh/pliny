from typing import Any

from neo4j import AsyncDriver

UPSERT_ITEM = """
MERGE (i:Item {id: $id})
SET i.title = $title, i.captured_at = $captured_at, i.type = $type
"""

DELETE_MENTIONS = """
MATCH (i:Item {id: $id})-[r:MENTIONS]->() DELETE r
"""

UPSERT_ENTITY_AND_MENTION = """
MERGE (e:Entity {id: $entity_id})
SET e.canonical_name = $canonical_name, e.type = $entity_type
WITH e
MATCH (i:Item {id: $item_id})
MERGE (i)-[r:MENTIONS]->(e)
SET r.confidence = $confidence
"""


async def upsert_item_with_mentions(
    driver: AsyncDriver,
    *,
    item: dict[str, Any],
    mentions: list[dict[str, Any]],
) -> None:
    """Idempotently upsert an Item node and its MENTIONS edges to entities.

    Stale MENTIONS for the item are wiped before re-creating, so reprocess
    cleanly removes entities the latest extraction no longer mentions.
    """
    async with driver.session() as session:
        await session.run(UPSERT_ITEM, **item)
        await session.run(DELETE_MENTIONS, id=item["id"])
        for m in mentions:
            await session.run(UPSERT_ENTITY_AND_MENTION, item_id=item["id"], **m)
