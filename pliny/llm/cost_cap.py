"""Postgres-backed daily LLM spend cap.

Pinned per-model prices for cost estimation. Source: OpenAI's published prices
checked 2026-04-28; revisit when bumping a model snapshot.
"""

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.llm.base import CostCapExceeded

PRICES_USD_PER_M_TOKENS: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o-mini-2024-07-18": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "text-embedding-3-small": (Decimal("0.02"), Decimal("0.02")),
}


def estimate_cost_usd(model: str, *, prompt_tokens: int, completion_tokens: int) -> Decimal:
    in_price, out_price = PRICES_USD_PER_M_TOKENS.get(model, (Decimal("0"), Decimal("0")))
    cost = (Decimal(prompt_tokens) * in_price + Decimal(completion_tokens) * out_price) / Decimal(
        "1000000"
    )
    return cost.quantize(Decimal("0.0001"))


async def record_spend(session: AsyncSession, usd: Decimal) -> None:
    await session.execute(
        text(
            """
            INSERT INTO llm_spend_daily (date, usd_spent)
            VALUES (current_date, :usd)
            ON CONFLICT (date) DO UPDATE
              SET usd_spent = llm_spend_daily.usd_spent + EXCLUDED.usd_spent
            """
        ),
        {"usd": usd},
    )
    await session.commit()


async def current_spend(session: AsyncSession) -> Decimal:
    row = await session.execute(
        text("SELECT usd_spent FROM llm_spend_daily WHERE date = current_date"),
    )
    val = row.scalar_one_or_none()
    return Decimal(0) if val is None else val


async def check_cap(session: AsyncSession, cap_usd: Decimal) -> None:
    spent = await current_spend(session)
    if spent >= cap_usd:
        raise CostCapExceeded(f"daily LLM cap of ${cap_usd} reached (spent ${spent})")
