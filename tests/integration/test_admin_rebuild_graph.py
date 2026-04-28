import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.db.queries import insert_item


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, item_entities, entities, item_tags, tags, items "
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def _count(driver: Any, cypher: str) -> int:
    async with driver.session() as s:
        result = await s.run(cypher)
        record = await result.single()
        assert record is not None
        return int(record["c"])


async def _seed(
    db_session: AsyncSession,
    *,
    items_with_entities: list[tuple[str, list[tuple[str, str]]]],
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    item_ids: list[uuid.UUID] = []
    entity_lookup: dict[tuple[str, str], uuid.UUID] = {}
    for title, entity_specs in items_with_entities:
        item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
        await db_session.execute(
            text("UPDATE items SET title = :t WHERE id = :id"),
            {"t": title, "id": item.id},
        )
        item_ids.append(item.id)
        for name, etype in entity_specs:
            key = (name, etype)
            if key not in entity_lookup:
                entity_id = uuid.uuid4()
                await db_session.execute(
                    text("INSERT INTO entities (id, canonical_name, type) VALUES (:i, :n, :t)"),
                    {"i": entity_id, "n": name, "t": etype},
                )
                entity_lookup[key] = entity_id
            await db_session.execute(
                text(
                    "INSERT INTO item_entities "
                    "(item_id, entity_id, mention_text, confidence, entities_version) "
                    "VALUES (:i, :e, :m, 0.9, 1)"
                ),
                {"i": item.id, "e": entity_lookup[key], "m": name},
            )
    await db_session.commit()
    return item_ids, list(entity_lookup.values())


async def test_rebuild_empty_db(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    neo4j_driver: Any,
) -> None:
    await _truncate(db_session)
    r = await client.post("/v1/admin/rebuild_graph", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"items": 0, "entities": 0, "mentions": 0, "related_to": 0}


async def test_rebuild_populates_neo4j(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    neo4j_driver: Any,
) -> None:
    await _truncate(db_session)
    await _seed(
        db_session,
        items_with_entities=[
            ("First", [("alice", "person"), ("acme", "org")]),
            ("Second", [("alice", "person"), ("beta", "concept")]),
        ],
    )
    r = await client.post("/v1/admin/rebuild_graph", headers=auth_headers)
    assert r.status_code == 200
    counts = r.json()
    assert counts == {"items": 2, "entities": 3, "mentions": 4, "related_to": 2}

    assert await _count(neo4j_driver, "MATCH (i:Item) RETURN count(i) AS c") == 2
    assert await _count(neo4j_driver, "MATCH (e:Entity) RETURN count(e) AS c") == 3
    assert await _count(neo4j_driver, "MATCH ()-[r:MENTIONS]->() RETURN count(r) AS c") == 4
    assert (
        await _count(
            neo4j_driver,
            "MATCH ()-[r:RELATED_TO {source:'cooccurrence'}]->() RETURN count(r) AS c",
        )
        == 2
    )


async def test_rebuild_idempotent(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    neo4j_driver: Any,
) -> None:
    await _truncate(db_session)
    await _seed(
        db_session,
        items_with_entities=[("T", [("alice", "person"), ("bob", "person")])],
    )
    r1 = await client.post("/v1/admin/rebuild_graph", headers=auth_headers)
    r2 = await client.post("/v1/admin/rebuild_graph", headers=auth_headers)
    assert r1.json() == r2.json()

    assert await _count(neo4j_driver, "MATCH (i:Item) RETURN count(i) AS c") == 1
    assert await _count(neo4j_driver, "MATCH (e:Entity) RETURN count(e) AS c") == 2
    assert await _count(neo4j_driver, "MATCH ()-[r:MENTIONS]->() RETURN count(r) AS c") == 2
    assert await _count(neo4j_driver, "MATCH ()-[r:RELATED_TO]->() RETURN count(r) AS c") == 1


async def test_rebuild_wipes_pre_existing_state(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    neo4j_driver: Any,
) -> None:
    await _truncate(db_session)
    async with neo4j_driver.session() as s:
        await s.run("CREATE (:StrayLabel {id: 'x'})")
        await s.run("CREATE (:Item {id: 'orphan-item'})")

    r = await client.post("/v1/admin/rebuild_graph", headers=auth_headers)
    assert r.status_code == 200

    assert await _count(neo4j_driver, "MATCH (n) RETURN count(n) AS c") == 0


async def test_rebuild_cooccurrence_canonical_direction(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    neo4j_driver: Any,
) -> None:
    await _truncate(db_session)
    await _seed(
        db_session,
        items_with_entities=[("T", [("alice", "person"), ("bob", "person")])],
    )
    await client.post("/v1/admin/rebuild_graph", headers=auth_headers)

    # only one direction recorded
    async with neo4j_driver.session() as s:
        result = await s.run("MATCH (a)-[r:RELATED_TO]->(b) RETURN a.id AS f, b.id AS t")
        rows = [(r["f"], r["t"]) async for r in result]
    assert len(rows) == 1
    f, t = rows[0]
    assert f < t  # canonical from < to


async def test_rebuild_requires_auth(
    client: AsyncClient,
) -> None:
    r = await client.post("/v1/admin/rebuild_graph")
    assert r.status_code == 401
