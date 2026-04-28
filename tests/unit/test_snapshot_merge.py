"""Unit-level coverage of merge edge cases that depend on prior redirect chains.

The redirect-collision path is exercised end-to-end in
`tests/integration/test_snapshot_redirect_collision.py`. This file pins down
the chained-merge case (an existing redirect's `to_id` gets re-targeted)
which is awkward to set up via the handler.
"""

import hashlib
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.api import deps
from pliny.db.models import ItemRedirect
from pliny.db.queries import insert_item
from pliny.snapshot.merge import merge_into_survivor


def _hash_url(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, item_entities, entities, item_tags, tags, items "
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def test_chained_merge_repoints_existing_redirects(
    db_session: AsyncSession,
) -> None:
    """If A previously redirected to B, and now B is being merged into C, the
    A->B row is re-targeted to A->C before B is deleted (otherwise the
    cascade would drop A's redirect entirely)."""
    await _truncate(db_session)
    a_id = uuid.uuid4()
    b = await insert_item(db_session, type="url", content_hash=_hash_url("b"))
    c = await insert_item(db_session, type="url", content_hash=_hash_url("c"))
    await db_session.execute(
        text(
            "INSERT INTO item_redirects (from_id, to_id, reason) "
            "VALUES (:f, :t, 'redirect_collision')"
        ),
        {"f": a_id, "t": b.id},
    )
    await db_session.commit()

    await merge_into_survivor(
        db_session,
        blob=deps.get_blob(),
        from_item_id=b.id,
        survivor=c,
        snapshot_artifacts=None,
    )
    await db_session.commit()

    rows = (
        (await db_session.execute(select(ItemRedirect).order_by(ItemRedirect.from_id)))
        .scalars()
        .all()
    )
    by_from = {r.from_id: r.to_id for r in rows}
    assert by_from[a_id] == c.id  # repointed
    assert by_from[b.id] == c.id  # new merge row
