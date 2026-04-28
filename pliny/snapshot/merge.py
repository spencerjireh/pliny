import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.db.models import Item
from pliny.db.queries import enqueue_job, notify
from pliny.storage.blob import BlobStore


@dataclass(frozen=True)
class SnapshotArtifacts:
    raw_ref: str
    screenshot_png: bytes | None
    metadata: dict[str, Any]


async def merge_into_survivor(
    db: AsyncSession,
    *,
    blob: BlobStore,
    from_item_id: uuid.UUID,
    survivor: Item,
    snapshot_artifacts: SnapshotArtifacts | None,
) -> None:
    """Merge a redirect-colliding item into an existing survivor.

    Transfers `item_sources`, re-points any prior `item_redirects.to_id` chains
    that pointed at the from-item, inserts a `redirect_collision` row, and
    (when the survivor has no snapshot yet) copies the freshly captured
    artifacts onto the survivor and enqueues `extract` for it.

    Finally deletes the from-item. The cascade prunes its `processing_jobs`,
    `item_sources`, and any other dependent rows. The `item_redirects` row
    inserted here has no FK on `from_id`, so it survives the delete and the
    `/v1/items/:id/status` endpoint returns `{redirect_to: ...}` for the
    merged-away id.
    """
    await db.execute(
        sql_text(
            """
            INSERT INTO item_sources (id, item_id, source, source_ref, captured_at)
            SELECT gen_random_uuid(), :survivor, source, source_ref, captured_at
              FROM item_sources WHERE item_id = :from_id
              ON CONFLICT (item_id, source, source_ref)
                WHERE source_ref IS NOT NULL
                DO NOTHING
            """
        ),
        {"survivor": survivor.id, "from_id": from_item_id},
    )

    await db.execute(
        sql_text("UPDATE item_redirects SET to_id = :survivor WHERE to_id = :from_id"),
        {"survivor": survivor.id, "from_id": from_item_id},
    )

    await db.execute(
        sql_text(
            """
            INSERT INTO item_redirects (from_id, to_id, reason)
            VALUES (:from_id, :survivor, 'redirect_collision')
            ON CONFLICT (from_id) DO NOTHING
            """
        ),
        {"survivor": survivor.id, "from_id": from_item_id},
    )

    if survivor.snapshot_version == 0 and snapshot_artifacts is not None:
        if snapshot_artifacts.screenshot_png is not None:
            await blob.put(
                f"derived/{survivor.id}/screenshot.png",
                snapshot_artifacts.screenshot_png,
            )
        await blob.put(
            f"derived/{survivor.id}/metadata.json",
            json.dumps(snapshot_artifacts.metadata).encode("utf-8"),
        )
        await db.execute(
            sql_text(
                """
                UPDATE items
                   SET raw_ref = :raw_ref,
                       metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:meta AS jsonb),
                       snapshot_version = 1
                 WHERE id = :id
                """
            ),
            {
                "raw_ref": snapshot_artifacts.raw_ref,
                "meta": json.dumps(snapshot_artifacts.metadata),
                "id": survivor.id,
            },
        )
        enqueued = await enqueue_job(db, item_id=survivor.id, stage="extract", pool="fast")
        if enqueued:
            await notify(db, "job_pool_fast", str(survivor.id))

    await db.execute(
        sql_text("DELETE FROM items WHERE id = :id"),
        {"id": from_item_id},
    )
