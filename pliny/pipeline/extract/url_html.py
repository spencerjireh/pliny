import hashlib

import httpx
import trafilatura
from sqlalchemy import text as sql_text

from pliny.db.models import Item
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import STAGE_VERSIONS

_USER_AGENT = "pliny/0.1 (+https://example.invalid/pliny)"
_TIMEOUT = 30.0


async def run(ctx: StageContext, item: Item) -> None:
    if item.canonical_url is None:
        raise ValueError(f"url item {ctx.item_id} has no canonical_url")

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=_TIMEOUT,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        resp = await client.get(item.canonical_url)
        resp.raise_for_status()
        body_bytes = resp.content

    if item.raw_ref is None:
        new_hash = hashlib.sha256(body_bytes).hexdigest()
        ref = f"raw/{new_hash}"
        await ctx.blob.put(ref, body_bytes)
        await ctx.db.execute(
            sql_text("UPDATE items SET raw_ref = :ref WHERE id = :id"),
            {"ref": ref, "id": ctx.item_id},
        )

    html = body_bytes.decode("utf-8", errors="replace")
    extracted = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        no_fallback=False,
    )

    await ctx.db.execute(
        sql_text(
            """
            INSERT INTO content (item_id, extracted_text, language, extraction_method,
                                 extract_version)
            VALUES (:id, :text, :lang, 'trafilatura', :version)
            ON CONFLICT (item_id) DO UPDATE SET
              extracted_text = EXCLUDED.extracted_text,
              language = EXCLUDED.language,
              extraction_method = EXCLUDED.extraction_method,
              extract_version = EXCLUDED.extract_version,
              extracted_at = now()
            """
        ),
        {
            "id": ctx.item_id,
            "text": extracted or "",
            "lang": None,
            "version": STAGE_VERSIONS["extract"],
        },
    )
