import uuid

from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.db.models import ProcessingJob
from pliny.db.queries import enqueue_job, insert_item


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def _seed_item(db_session: AsyncSession, *, summarize_version: int = 0) -> uuid.UUID:
    item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    if summarize_version != 0:
        await db_session.execute(
            text("UPDATE items SET summarize_version=:v WHERE id=:id"),
            {"v": summarize_version, "id": item.id},
        )
    await db_session.commit()
    return item.id


async def test_reprocess_stage_resets_old_versions(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """Items with summarize_version < current should have their job rows reset."""
    await _truncate(db_session)

    # Item already at summarize_version=1 (the current constant).
    item_at_current = await _seed_item(db_session, summarize_version=1)
    await enqueue_job(db_session, item_id=item_at_current, stage="summarize", pool="fast")
    await db_session.execute(
        text("UPDATE processing_jobs SET status='done' WHERE item_id=:id"),
        {"id": item_at_current},
    )

    # Item that's behind the current version (will be reset).
    item_behind = await _seed_item(db_session, summarize_version=0)
    await enqueue_job(db_session, item_id=item_behind, stage="summarize", pool="fast")
    await db_session.execute(
        text("UPDATE processing_jobs SET status='done' WHERE item_id=:id"),
        {"id": item_behind},
    )
    await db_session.commit()

    # Bump the in-code constant so that summarize_version=1 is "behind".
    from pliny.api.routes import admin as admin_mod
    from pliny.pipeline import stages as stage_mod

    monkeypatch.setitem(stage_mod.STAGE_VERSIONS, "summarize", 2)
    monkeypatch.setitem(admin_mod.STAGE_VERSIONS, "summarize", 2)

    r = await client.post("/v1/admin/reprocess?stage=summarize", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    # Both items have version < 2, so both rows reset; nothing queued (rows existed).
    assert body == {"reset": 2, "queued": 0}

    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.item_id == item_behind))
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0


async def test_reprocess_stage_inserts_missing_jobs(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """Items below current version with no job row should get one inserted."""
    await _truncate(db_session)
    item_id = await _seed_item(db_session, summarize_version=0)

    from pliny.api.routes import admin as admin_mod
    from pliny.pipeline import stages as stage_mod

    monkeypatch.setitem(stage_mod.STAGE_VERSIONS, "summarize", 2)
    monkeypatch.setitem(admin_mod.STAGE_VERSIONS, "summarize", 2)

    r = await client.post("/v1/admin/reprocess?stage=summarize", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"reset": 0, "queued": 1}

    job = (
        await db_session.execute(
            select(ProcessingJob).where(
                ProcessingJob.item_id == item_id, ProcessingJob.stage == "summarize"
            )
        )
    ).scalar_one()
    assert job.status == "pending"
    assert job.pool == "fast"


async def test_reprocess_stage_skips_items_at_current(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """When all items are at the current version, the bulk reprocess is a no-op."""
    await _truncate(db_session)
    item_id = await _seed_item(db_session, summarize_version=1)
    await enqueue_job(db_session, item_id=item_id, stage="summarize", pool="fast")
    await db_session.execute(
        text("UPDATE processing_jobs SET status='done' WHERE item_id=:id"),
        {"id": item_id},
    )
    await db_session.commit()

    r = await client.post("/v1/admin/reprocess?stage=summarize", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"reset": 0, "queued": 0}

    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.item_id == item_id))
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "done"


async def test_reprocess_stage_rejects_unknown_stage(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    r = await client.post("/v1/admin/reprocess?stage=bogus", headers=auth_headers)
    assert r.status_code == 400


async def test_reprocess_stage_requires_auth(client: AsyncClient) -> None:
    r = await client.post("/v1/admin/reprocess?stage=summarize")
    assert r.status_code == 401


async def test_reprocess_item_stage_inserts_when_missing(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    item_id = await _seed_item(db_session)

    r = await client.post(
        f"/v1/admin/items/{item_id}/reprocess?stage=extract", headers=auth_headers
    )
    assert r.status_code == 200
    assert r.json() == {"status": "pending"}

    job = (
        await db_session.execute(
            select(ProcessingJob).where(
                ProcessingJob.item_id == item_id, ProcessingJob.stage == "extract"
            )
        )
    ).scalar_one()
    assert job.status == "pending"
    assert job.pool == "fast"


async def test_reprocess_item_stage_resets_existing(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    item_id = await _seed_item(db_session)
    await enqueue_job(db_session, item_id=item_id, stage="extract", pool="fast")
    await db_session.execute(
        text(
            "UPDATE processing_jobs SET status='failed', error='boom', attempts=6 WHERE item_id=:id"
        ),
        {"id": item_id},
    )
    await db_session.commit()

    r = await client.post(
        f"/v1/admin/items/{item_id}/reprocess?stage=extract", headers=auth_headers
    )
    assert r.status_code == 200

    job = (
        await db_session.execute(select(ProcessingJob).where(ProcessingJob.item_id == item_id))
    ).scalar_one()
    await db_session.refresh(job)
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.error is None


async def test_reprocess_item_stage_404_when_missing(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    r = await client.post(
        f"/v1/admin/items/{uuid.uuid4()}/reprocess?stage=extract", headers=auth_headers
    )
    assert r.status_code == 404


async def test_reprocess_item_stage_rejects_unknown_stage(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    item_id = await _seed_item(db_session)
    r = await client.post(f"/v1/admin/items/{item_id}/reprocess?stage=bogus", headers=auth_headers)
    assert r.status_code == 400


async def test_reprocess_item_stage_requires_auth(client: AsyncClient) -> None:
    r = await client.post(f"/v1/admin/items/{uuid.uuid4()}/reprocess?stage=extract")
    assert r.status_code == 401
