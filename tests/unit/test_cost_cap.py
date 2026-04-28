from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.llm.base import CostCapExceeded
from pliny.llm.cost_cap import (
    check_cap,
    current_spend,
    estimate_cost_usd,
    record_spend,
)


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(text("DELETE FROM llm_spend_daily"))
    await db_session.commit()


def test_estimate_cost_known_model() -> None:
    cost = estimate_cost_usd(
        "gpt-4o-mini-2024-07-18", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == Decimal("0.7500")


def test_estimate_cost_unknown_model_returns_zero() -> None:
    cost = estimate_cost_usd("not-a-real-model", prompt_tokens=1000, completion_tokens=1000)
    assert cost == Decimal("0.0000")


async def test_record_and_current_spend(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    await record_spend(db_session, Decimal("1.25"))
    await record_spend(db_session, Decimal("0.50"))
    spent = await current_spend(db_session)
    assert spent == Decimal("1.7500")


async def test_check_cap_passes_below_threshold(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    await record_spend(db_session, Decimal("0.10"))
    await check_cap(db_session, Decimal("1.00"))


async def test_check_cap_raises_at_threshold(db_session: AsyncSession) -> None:
    await _truncate(db_session)
    await record_spend(db_session, Decimal("1.00"))
    with pytest.raises(CostCapExceeded):
        await check_cap(db_session, Decimal("1.00"))
