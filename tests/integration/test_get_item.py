import uuid

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.db.models import (
    Chunk,
    Content,
    Entity,
    Item,
    ItemEntity,
    ItemRedirect,
    ItemTag,
    Tag,
)


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, item_entities, entities, item_tags, tags, "
            "embeddings_1536, chunks, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def test_get_item_basic(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    payload = {"text": "hello world", "source": "api", "source_ref": "g-1"}
    r = await client.post("/v1/items", json=payload, headers=auth_headers)
    assert r.status_code == 202
    item_id = r.json()["items"][0]["item_id"]

    r = await client.get(f"/v1/items/{item_id}", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == item_id
    assert body["type"] == "text"
    assert body["title"] is None
    assert body["summary"] is None
    assert body["content"] is None
    assert body["chunks"] == []
    assert body["entities"] == []
    assert body["tags"] == []
    assert any(s["source"] == "api" and s["source_ref"] == "g-1" for s in body["sources"])
    assert body["metadata"] == {}


async def test_get_item_with_derived_rows(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    payload = {"text": "lorem ipsum", "source": "api", "source_ref": "g-2"}
    r = await client.post("/v1/items", json=payload, headers=auth_headers)
    item_id_str = r.json()["items"][0]["item_id"]
    item_id = uuid.UUID(item_id_str)

    item = (
        await db_session.execute(text("SELECT id FROM items WHERE id = :id"), {"id": item_id})
    ).scalar_one()
    assert item == item_id

    db_session.add(
        Content(
            item_id=item_id,
            extracted_text="lorem ipsum",
            language="en",
            extraction_method="text",
            extract_version=1,
        )
    )
    db_session.add(
        Chunk(item_id=item_id, chunk_index=0, text="lorem ipsum", token_count=2, chunker_version=1)
    )
    entity = Entity(canonical_name="Lorem", type="concept")
    db_session.add(entity)
    await db_session.flush()
    db_session.add(
        ItemEntity(
            item_id=item_id,
            entity_id=entity.id,
            mention_text="lorem",
            confidence=0.9,
            entities_version=1,
        )
    )
    tag = Tag(name="placeholder")
    db_session.add(tag)
    await db_session.flush()
    db_session.add(ItemTag(item_id=item_id, tag_id=tag.id))

    item_obj = (
        (await db_session.execute(text("SELECT * FROM items WHERE id = :id"), {"id": item_id}))
        .mappings()
        .one()
    )
    assert item_obj["id"] == item_id

    db_item = await db_session.get(Item, item_id)
    assert db_item is not None
    db_item.title = "Hello"
    db_item.summary = "A short summary."
    db_item.meta = {"foo": "bar"}
    await db_session.commit()

    r = await client.get(f"/v1/items/{item_id}", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Hello"
    assert body["summary"] == "A short summary."
    assert body["content"] == {"extracted_text": "lorem ipsum"}
    assert body["chunks"] == [{"index": 0, "text": "lorem ipsum"}]
    assert body["entities"] == [
        {"name": "Lorem", "type": "concept", "mention_text": "lorem", "confidence": 0.9}
    ]
    assert body["tags"] == ["placeholder"]
    assert body["metadata"] == {"foo": "bar"}


async def test_get_item_redirected(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    r = await client.post(
        "/v1/items",
        json={"text": "survivor", "source": "api", "source_ref": "g-survivor"},
        headers=auth_headers,
    )
    survivor_id = r.json()["items"][0]["item_id"]

    from_id = uuid.uuid4()
    db_session.add(
        ItemRedirect(from_id=from_id, to_id=uuid.UUID(survivor_id), reason="redirect_collision")
    )
    await db_session.commit()

    r = await client.get(f"/v1/items/{from_id}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"redirect_to": survivor_id}


async def test_get_item_not_found(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    r = await client.get(f"/v1/items/{uuid.uuid4()}", headers=auth_headers)
    assert r.status_code == 404
