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


async def test_metrics_returns_prometheus_text(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Empty DB still emits help/type lines for the registered gauges."""
    await _truncate(db_session)
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "# HELP pliny_processing_jobs" in body
    assert "# TYPE pliny_processing_jobs gauge" in body
    assert "# HELP pliny_stage_lag_seconds" in body
    assert "# HELP pliny_stage_errors" in body


async def test_metrics_reflects_seeded_jobs(client: AsyncClient, db_session: AsyncSession) -> None:
    await _truncate(db_session)
    item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    await enqueue_job(db_session, item_id=item.id, stage="extract", pool="fast")
    await db_session.execute(
        text(
            "UPDATE processing_jobs SET status='failed', error='boom', attempts=6 WHERE item_id=:id"
        ),
        {"id": item.id},
    )
    await db_session.commit()

    r = await client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert 'pliny_processing_jobs{pool="fast",status="failed"} 1.0' in body
    assert 'pliny_stage_errors{stage="extract"} 1.0' in body


async def test_metrics_does_not_require_auth(client: AsyncClient) -> None:
    r = await client.get("/metrics")
    assert r.status_code == 200


async def test_metrics_pending_lag_is_emitted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    item = await insert_item(db_session, type="text", content_hash=uuid.uuid4().hex)
    await enqueue_job(db_session, item_id=item.id, stage="summarize", pool="fast")
    await db_session.execute(
        text(
            "UPDATE processing_jobs SET next_attempt_at = now() - interval '5 seconds' "
            "WHERE item_id=:id"
        ),
        {"id": item.id},
    )
    await db_session.commit()

    r = await client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    # Lag is non-zero seconds for the seeded pending job.
    assert 'pliny_stage_lag_seconds{stage="summarize"}' in body
