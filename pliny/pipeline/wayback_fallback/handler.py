import hashlib

import httpx
from sqlalchemy import text as sql_text

from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import register

WAYBACK_AVAILABILITY = "https://archive.org/wayback/available"
_USER_AGENT = "pliny/0.1 (+https://example.invalid/pliny)"
_HTTP_TIMEOUT = 30.0


class WaybackUnavailable(Exception):
    """Raised when the Wayback Machine has no archived snapshot for the URL."""


@register("wayback_fallback")
async def wayback_fallback_handler(ctx: StageContext) -> None:
    """Recover from a failed snapshot by pulling the closest Wayback archive.

    Triggered by the runner when `snapshot` exhausts its retries. Queries the
    availability API for `items.canonical_url`; if a closest snapshot exists,
    fetches that archived HTML, writes it to raw/<sha>, updates
    `items.raw_ref` and tags `metadata.archive_source='wayback'`. Downstream
    `extract` then runs as usual.

    On no archive (or non-200 archived status), raises so the job fails
    terminally per the standard backoff policy.
    """
    row = (
        (
            await ctx.db.execute(
                sql_text("SELECT canonical_url FROM items WHERE id = :id"),
                {"id": ctx.item_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RuntimeError(f"wayback_fallback: item {ctx.item_id} not found")
    canonical_url = row["canonical_url"]
    if not canonical_url:
        raise RuntimeError(f"wayback_fallback: item {ctx.item_id} has no canonical_url")

    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        avail = await client.get(WAYBACK_AVAILABILITY, params={"url": canonical_url})
        avail.raise_for_status()
        snap = avail.json().get("archived_snapshots", {}).get("closest")
        if not snap or str(snap.get("status")) != "200":
            raise WaybackUnavailable(canonical_url)
        archive_url = snap["url"]
        ts = snap.get("timestamp")

        archived = await client.get(archive_url, follow_redirects=True)
        archived.raise_for_status()
        body = archived.content

    raw_hash = hashlib.sha256(body).hexdigest()
    raw_ref = f"raw/{raw_hash}"
    await ctx.blob.put(raw_ref, body)

    await ctx.db.execute(
        sql_text(
            """
            UPDATE items
               SET raw_ref = :ref,
                   metadata = COALESCE(metadata, '{}'::jsonb) ||
                              jsonb_build_object('archive_source', 'wayback',
                                                 'archive_timestamp', CAST(:ts AS text))
             WHERE id = :id
            """
        ),
        {"ref": raw_ref, "ts": ts, "id": ctx.item_id},
    )
