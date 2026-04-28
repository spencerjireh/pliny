import uuid

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.db.queries import enqueue_job, insert_item


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def test_status_pending_for_just_ingested(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    payload = {"text": "hello", "source": "api", "source_ref": "s-1"}
    r = await client.post("/v1/items", json=payload, headers=auth_headers)
    item_id = r.json()["items"][0]["item_id"]

    r = await client.get(f"/v1/items/{item_id}/status", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == item_id
    assert "snapshot" not in body["stages"]
    assert body["stages"]["extract"]["status"] == "pending"
    for stage in ("summarize", "chunk", "embed", "entities", "graph_sync"):
        assert body["stages"][stage]["status"] == "pending"
    assert body["overall"] == "processing"


async def test_status_url_includes_snapshot(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    payload = {"url": "https://example.com/x", "source": "api", "source_ref": "u-1"}
    r = await client.post("/v1/items", json=payload, headers=auth_headers)
    item_id = r.json()["items"][0]["item_id"]
    r = await client.get(f"/v1/items/{item_id}/status", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert "snapshot" in body["stages"]
    assert body["stages"]["snapshot"]["status"] == "pending"


async def test_status_done_after_version_bump(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    await db_session.execute(
        text("UPDATE items SET extract_version = 1 WHERE id = :id"),
        {"id": item.id},
    )
    await db_session.commit()

    r = await client.get(f"/v1/items/{item.id}/status", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["stages"]["extract"]["status"] == "done"
    assert body["stages"]["extract"]["version"] == 1


async def test_status_failed_propagates(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    await enqueue_job(db_session, item_id=item.id, stage="extract", pool="fast")
    await db_session.execute(
        text("UPDATE processing_jobs SET status='failed', error='boom' WHERE item_id = :id"),
        {"id": item.id},
    )
    await db_session.commit()

    r = await client.get(f"/v1/items/{item.id}/status", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["stages"]["extract"]["status"] == "failed"
    assert body["stages"]["extract"]["error"] == "boom"
    assert body["overall"] == "failed"


async def test_status_redirect(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    survivor = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    from_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO item_redirects (from_id, to_id, reason) "
            "VALUES (:from_id, :to_id, 'redirect_collision')"
        ),
        {"from_id": from_id, "to_id": survivor.id},
    )
    await db_session.commit()

    r = await client.get(f"/v1/items/{from_id}/status", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert "stages" not in body
    assert body["redirect_to"] == str(survivor.id)


async def test_status_unknown_id_404(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    r = await client.get(f"/v1/items/{uuid.uuid4()}/status", headers=auth_headers)
    assert r.status_code == 404


async def test_status_ready_when_all_versions_set(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    await db_session.execute(
        text(
            "UPDATE items SET extract_version=1, summarize_version=1, chunk_version=1, "
            "embed_version=1, entities_version=1, graph_sync_version=1 WHERE id = :id"
        ),
        {"id": item.id},
    )
    await db_session.commit()
    r = await client.get(f"/v1/items/{item.id}/status", headers=auth_headers)
    body = r.json()
    assert body["overall"] == "ready"
