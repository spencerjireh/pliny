from typing import Any, cast

from neo4j import AsyncDriver
from sqlalchemy import text as sql_text

from pliny.graph.schema import ensure_constraints
from pliny.graph.sync import upsert_item_with_mentions
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import register

VERSION = 1


@register("graph_sync")
async def graph_sync_handler(ctx: StageContext) -> None:
    if ctx.neo4j is None:
        raise RuntimeError("Neo4j driver required for graph_sync stage")
    driver = cast(AsyncDriver, ctx.neo4j)
    await ensure_constraints(driver)

    item_row = (
        (
            await ctx.db.execute(
                sql_text("SELECT id, title, captured_at, type FROM items WHERE id = :id"),
                {"id": ctx.item_id},
            )
        )
        .mappings()
        .one()
    )

    mention_rows = (
        (
            await ctx.db.execute(
                sql_text(
                    "SELECT ie.entity_id, ie.confidence, "
                    "       e.canonical_name, e.type AS entity_type "
                    "FROM item_entities ie JOIN entities e ON e.id = ie.entity_id "
                    "WHERE ie.item_id = :id"
                ),
                {"id": ctx.item_id},
            )
        )
        .mappings()
        .all()
    )

    item: dict[str, Any] = {
        "id": str(item_row["id"]),
        "title": item_row["title"] or "",
        "captured_at": (item_row["captured_at"].isoformat() if item_row["captured_at"] else None),
        "type": item_row["type"],
    }
    mentions: list[dict[str, Any]] = [
        {
            "entity_id": str(m["entity_id"]),
            "canonical_name": m["canonical_name"],
            "entity_type": m["entity_type"],
            "confidence": float(m["confidence"]) if m["confidence"] is not None else None,
        }
        for m in mention_rows
    ]

    await upsert_item_with_mentions(driver, item=item, mentions=mentions)
