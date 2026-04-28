from sqlalchemy import text as sql_text

from pliny.db.models import Item
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import STAGE_VERSIONS


async def run(ctx: StageContext, item: Item) -> None:
    if item.raw_ref is None:
        raise ValueError(f"text item {ctx.item_id} has no raw_ref")
    raw = await ctx.blob.get(item.raw_ref)
    extracted = raw.decode("utf-8", errors="replace")

    await ctx.db.execute(
        sql_text(
            """
            INSERT INTO content (item_id, extracted_text, language, extraction_method,
                                 extract_version)
            VALUES (:id, :text, :lang, 'identity', :version)
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
            "text": extracted,
            "lang": None,
            "version": STAGE_VERSIONS["extract"],
        },
    )
