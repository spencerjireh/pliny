import trafilatura
from sqlalchemy import text as sql_text

from pliny.db.models import Item
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import STAGE_VERSIONS


async def run(ctx: StageContext, item: Item) -> None:
    """Extract text from the SingleFile HTML the snapshot stage stored at raw_ref.

    URL items always go through `snapshot` first, which is responsible for
    fetching/rendering the page and writing rendered bytes to
    `items.raw_ref`. Extract reads those bytes and runs trafilatura.
    """
    if item.raw_ref is None:
        raise RuntimeError(f"url item {ctx.item_id} has no raw_ref; snapshot must run first")
    body_bytes = await ctx.blob.get(item.raw_ref)
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
