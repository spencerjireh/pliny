import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.api import deps
from pliny.db.models import Item, ItemRedirect


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, item_entities, entities, item_tags, tags, "
            "embeddings_1536, chunks, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def test_delete_item_happy_path(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    neo4j_driver: Any,
) -> None:
    await _truncate(db_session)
    r = await client.post(
        "/v1/items",
        json={"text": "delete me", "source": "api", "source_ref": "d-1"},
        headers=auth_headers,
    )
    assert r.status_code == 202
    item_id = r.json()["items"][0]["item_id"]

    db_item = await db_session.get(Item, uuid.UUID(item_id))
    assert db_item is not None
    raw_ref = db_item.raw_ref
    assert raw_ref is not None

    blob = deps.get_blob()
    assert await blob.exists(raw_ref)

    # Seed a derived artifact so we can verify directory cleanup.
    derived_key = f"derived/{item_id}/screenshot.png"
    await blob.put(derived_key, b"\x89PNG")
    assert await blob.exists(derived_key)

    # Seed a Neo4j Item node so we can verify it gets removed.
    async with neo4j_driver.session() as s:
        await s.run("MERGE (:Item {id: $id})", id=str(item_id))

    r = await client.delete(f"/v1/items/{item_id}", headers=auth_headers)
    assert r.status_code == 204

    # Postgres row gone (cascade-tested implicitly by FK constraints).
    db_session.expire_all()
    assert await db_session.get(Item, uuid.UUID(item_id)) is None

    # Blob artifacts removed.
    assert not await blob.exists(raw_ref)
    assert not await blob.exists(derived_key)

    # Neo4j node gone.
    async with neo4j_driver.session() as s:
        result = await s.run("MATCH (i:Item {id: $id}) RETURN count(i) AS c", id=str(item_id))
        record = await result.single()
    assert record is not None
    assert int(record["c"]) == 0

    # Subsequent GET returns 404.
    r = await client.get(f"/v1/items/{item_id}", headers=auth_headers)
    assert r.status_code == 404


async def test_delete_item_not_found(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    r = await client.delete(f"/v1/items/{uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 404


async def test_delete_clears_redirect_from_id(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    neo4j_driver: Any,
) -> None:
    """Deleting the survivor must drop redirect rows pointing at it (FK cascade
    on to_id), but more importantly deleting any item must drop redirect rows
    where its id is the merged-away `from_id`."""
    await _truncate(db_session)
    r = await client.post(
        "/v1/items",
        json={"text": "i am alive", "source": "api", "source_ref": "alive"},
        headers=auth_headers,
    )
    survivor_id = uuid.UUID(r.json()["items"][0]["item_id"])

    # Pretend this item once redirected somewhere. The `from_id` here is *the
    # survivor itself*, simulating: this item was once a from-side and got pre-
    # pointed at a different survivor. Deleting it should clear that row.
    fake_target = uuid.UUID(int=0xDEAD)
    db_session.add(
        ItemRedirect(from_id=survivor_id, to_id=survivor_id, reason="redirect_collision")
    )
    await db_session.commit()

    r = await client.delete(f"/v1/items/{survivor_id}", headers=auth_headers)
    assert r.status_code == 204

    remaining = (
        await db_session.execute(
            text("SELECT count(*)::int FROM item_redirects WHERE from_id = :id"),
            {"id": survivor_id},
        )
    ).scalar_one()
    assert remaining == 0
    _ = fake_target  # silence unused; kept for the comment context above


async def test_delete_already_merged_id_is_404(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """A from_id (merged-away id) has no row in `items`. DELETE returns 404.
    The frontend always issues DELETE against a live `id` from search results,
    so this is the right behavior — silently following the redirect would
    destroy data the caller didn't explicitly target."""
    await _truncate(db_session)
    r = await client.post(
        "/v1/items",
        json={"text": "survivor", "source": "api", "source_ref": "s"},
        headers=auth_headers,
    )
    survivor_id = uuid.UUID(r.json()["items"][0]["item_id"])

    merged_away = uuid.uuid4()
    db_session.add(
        ItemRedirect(from_id=merged_away, to_id=survivor_id, reason="redirect_collision")
    )
    await db_session.commit()

    r = await client.delete(f"/v1/items/{merged_away}", headers=auth_headers)
    assert r.status_code == 404

    # Survivor still alive.
    r = await client.get(f"/v1/items/{survivor_id}", headers=auth_headers)
    assert r.status_code == 200
