import hashlib
import json
from typing import Any

import httpx
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.canonicalize import canonicalize
from pliny.db.models import Item
from pliny.db.queries import find_item_by_content_hash
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import register
from pliny.snapshot.base import Snapshotter
from pliny.snapshot.classifier import Classification, classify_url
from pliny.snapshot.merge import SnapshotArtifacts, merge_into_survivor

_USER_AGENT = "pliny/0.1 (+https://example.invalid/pliny)"
_HTTP_TIMEOUT = 30.0


async def _load_item(db: AsyncSession, item_id: Any) -> Item:
    row = (
        (
            await db.execute(
                sql_text(
                    "SELECT id, type, canonical_url, content_hash, raw_ref, "
                    "snapshot_version, captured_at FROM items WHERE id = :id"
                ),
                {"id": item_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RuntimeError(f"snapshot: item {item_id} not found")
    item = Item()
    item.id = row["id"]
    item.type = row["type"]
    item.canonical_url = row["canonical_url"]
    item.content_hash = row["content_hash"]
    item.raw_ref = row["raw_ref"]
    item.snapshot_version = row["snapshot_version"]
    item.captured_at = row["captured_at"]
    return item


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_url(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


async def _capture_html(
    ctx: StageContext,
    *,
    classification: Classification,
) -> tuple[str, bytes | None, dict[str, Any]]:
    """HTML path: render via Snapshotter, return (raw_ref, screenshot_bytes, metadata)."""
    if ctx.snapshotter is None:
        raise RuntimeError("snapshot: snapshotter dependency missing")
    snapshotter: Snapshotter = ctx.snapshotter  # type: ignore[assignment]
    result = await snapshotter.capture_html(classification.final_url)
    raw_hash = _hash_bytes(result.rendered_html)
    raw_ref = f"raw/{raw_hash}"
    await ctx.blob.put(raw_ref, result.rendered_html)
    metadata: dict[str, Any] = {
        "final_url": result.final_url,
        "content_type": classification.content_type,
        "fetched_at": result.fetched_at.isoformat(),
        "page_title": result.page_title,
    }
    return raw_ref, result.screenshot_png, metadata


async def _capture_pdf(
    ctx: StageContext,
    *,
    client: httpx.AsyncClient,
    classification: Classification,
) -> tuple[str, bytes | None, dict[str, Any]]:
    """PDF path: fetch the bytes, write to raw/<sha>, no screenshot."""
    resp = await client.get(
        classification.final_url,
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    )
    resp.raise_for_status()
    body = resp.content
    raw_hash = _hash_bytes(body)
    raw_ref = f"raw/{raw_hash}"
    await ctx.blob.put(raw_ref, body)
    metadata: dict[str, Any] = {
        "final_url": classification.final_url,
        "content_type": classification.content_type,
        "fetched_at": _now_isoformat(),
    }
    return raw_ref, None, metadata


async def _capture_direct_media(
    ctx: StageContext,
    *,
    classification: Classification,
) -> tuple[str, bytes | None, dict[str, Any]]:
    """audio/video path: persist a metadata-only JSON as the canonical raw bytes
    (per spec line 519's "metadata JSON as raw bytes" pattern). No download —
    media files can be huge and the actual playback URL is the source of
    truth.
    """
    metadata: dict[str, Any] = {
        "final_url": classification.final_url,
        "content_type": classification.content_type,
        "fetched_at": _now_isoformat(),
    }
    body = json.dumps(metadata).encode("utf-8")
    raw_hash = _hash_bytes(body)
    raw_ref = f"raw/{raw_hash}"
    await ctx.blob.put(raw_ref, body)
    return raw_ref, None, metadata


def _now_isoformat() -> str:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC).isoformat()


@register("snapshot")
async def snapshot_handler(ctx: StageContext) -> None:
    item = await _load_item(ctx.db, ctx.item_id)
    if item.type != "url":
        raise RuntimeError(f"snapshot: item {ctx.item_id} is type={item.type!r}, expected 'url'")
    if not item.canonical_url:
        raise RuntimeError(f"snapshot: item {ctx.item_id} has no canonical_url")

    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        classification = await classify_url(item.canonical_url, client=client)

        new_canonical_url = canonicalize(classification.final_url)
        new_content_hash = _hash_url(new_canonical_url)

        if classification.bucket == "html":
            raw_ref, screenshot_bytes, metadata = await _capture_html(
                ctx, classification=classification
            )
        elif classification.bucket == "pdf":
            raw_ref, screenshot_bytes, metadata = await _capture_pdf(
                ctx, client=client, classification=classification
            )
        else:  # audio | video
            raw_ref, screenshot_bytes, metadata = await _capture_direct_media(
                ctx, classification=classification
            )

    new_type = classification.bucket if classification.bucket in ("audio", "video") else item.type

    # Collision check happens after capture but before any item-level UPDATE so the
    # merge path can transfer the freshly captured artifacts to the survivor.
    survivor = await find_item_by_content_hash(ctx.db, new_content_hash)
    if survivor is not None and survivor.id != item.id:
        await merge_into_survivor(
            ctx.db,
            blob=ctx.blob,
            from_item_id=item.id,
            survivor=survivor,
            snapshot_artifacts=SnapshotArtifacts(
                raw_ref=raw_ref,
                screenshot_png=screenshot_bytes,
                metadata=metadata,
            ),
        )
        ctx.skip_downstream = True
        return

    # No collision — write screenshot (HTML only), metadata.json, and update the item.
    if screenshot_bytes is not None:
        await ctx.blob.put(f"derived/{item.id}/screenshot.png", screenshot_bytes)
    await ctx.blob.put(
        f"derived/{item.id}/metadata.json",
        json.dumps(metadata).encode("utf-8"),
    )

    await ctx.db.execute(
        sql_text(
            """
            UPDATE items
               SET canonical_url = :canonical_url,
                   content_hash = :content_hash,
                   raw_ref = :raw_ref,
                   type = :type,
                   metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:meta AS jsonb)
             WHERE id = :id
            """
        ),
        {
            "canonical_url": new_canonical_url,
            "content_hash": new_content_hash,
            "raw_ref": raw_ref,
            "type": new_type,
            "meta": json.dumps(metadata),
            "id": item.id,
        },
    )
