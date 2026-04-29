import io

import imagehash
from PIL import Image
from sqlalchemy import text as sql_text

from pliny.db.models import Item
from pliny.llm.base import LLM
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import STAGE_VERSIONS
from pliny.prompts.vision_ocr import PROMPT
from pliny.prompts.vision_ocr import VERSION as PROMPT_VERSION

_VISION_MODEL = "gpt-4o-mini"
_HAMMING_THRESHOLD = 5
_BIT_MASK_64 = (1 << 64) - 1
_SIGN_BIT = 1 << 63


def _phash_int(raw: bytes) -> int:
    img = Image.open(io.BytesIO(raw))
    return int(str(imagehash.phash(img)), 16)


def _to_signed_64(unsigned: int) -> int:
    """Map a 64-bit unsigned int onto signed bigint range."""
    return unsigned - (1 << 64) if unsigned >= _SIGN_BIT else unsigned


def _from_signed_64(signed: int) -> int:
    return signed + (1 << 64) if signed < 0 else signed


async def _find_phash_duplicate(ctx: StageContext, *, phash_unsigned: int) -> str | None:
    rows = (
        await ctx.db.execute(
            sql_text("SELECT item_id, phash FROM image_phashes WHERE item_id != :id"),
            {"id": ctx.item_id},
        )
    ).all()
    for row_id, row_phash in rows:
        other = _from_signed_64(int(row_phash))
        diff = (phash_unsigned ^ other) & _BIT_MASK_64
        if bin(diff).count("1") <= _HAMMING_THRESHOLD:
            return str(row_id)
    return None


async def run(ctx: StageContext, item: Item) -> None:
    if item.raw_ref is None:
        raise ValueError(f"image item {ctx.item_id} has no raw_ref")
    if ctx.llm is None:
        raise RuntimeError("LLM client required for image extract")
    raw = await ctx.blob.get(item.raw_ref)

    mime = "image/png"
    if item.meta is not None:
        mime = item.meta.get("mime", mime)

    llm: LLM = ctx.llm  # type: ignore[assignment]
    response = await llm.vision(
        image_bytes=raw,
        image_mime=mime,
        prompt=PROMPT,
        model=_VISION_MODEL,
    )
    extracted = response.text

    phash_unsigned = _phash_int(raw)
    phash_signed = _to_signed_64(phash_unsigned)

    await ctx.db.execute(
        sql_text(
            """
            INSERT INTO image_phashes (item_id, phash)
            VALUES (:id, :ph)
            ON CONFLICT (item_id) DO UPDATE
              SET phash = EXCLUDED.phash, computed_at = now()
            """
        ),
        {"id": ctx.item_id, "ph": phash_signed},
    )

    duplicate_id = await _find_phash_duplicate(ctx, phash_unsigned=phash_unsigned)
    if duplicate_id is not None:
        await ctx.db.execute(
            sql_text(
                "UPDATE items SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('possible_duplicate_of', cast(:dup as text)) "
                "WHERE id = :id"
            ),
            {"id": ctx.item_id, "dup": duplicate_id},
        )

    method = f"vision:gpt-4o-mini@v{PROMPT_VERSION}"
    await ctx.db.execute(
        sql_text(
            """
            INSERT INTO content (item_id, extracted_text, language, extraction_method,
                                 extract_version)
            VALUES (:id, :text, :lang, :method, :version)
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
            "method": method,
            "version": STAGE_VERSIONS["extract"],
        },
    )
