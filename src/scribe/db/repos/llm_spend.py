"""``llm_spend`` (issue #67): per-user daily LLM spend by route. Counts and money only.

- :func:`add` upserts one call into its (user, UTC day, route) row, adding to the counters.
- :class:`SpendRecorder` is the router's ``spend_sink``: one short transaction per call.
- :func:`daily` reads the rows from a day on, for ``python -m scribe.admin cost``.
- :func:`spent_on` is one user's total of one day (issue #74, the budget check before each turn:
  one query on the ``(user_id, day, route)`` unique index); :func:`totals_on` is every user's
  total of one day (the operator's daily spend alert).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.db.models import LLMSpend
from scribe.llm.spend import SpendEntry

COUNTERS = (
    "calls",
    "failed_calls",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "cache_write_1h_input_tokens",
    "cost_usd",
)


def spend_day(entry: SpendEntry) -> date:
    at = entry.at.astimezone(UTC) if entry.at.tzinfo is not None else entry.at
    return at.date()


async def add(session: AsyncSession, entry: SpendEntry) -> None:
    usage = entry.usage
    values = {
        "calls": 1,
        "failed_calls": 1 if entry.failed else 0,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "cache_write_1h_input_tokens": usage.cache_write_1h_input_tokens,
        "cost_usd": Decimal(str(entry.cost_usd)),
    }
    stmt = insert(LLMSpend).values(
        id=uuid.uuid4(), user_id=entry.user_id, day=spend_day(entry), route=entry.route, **values
    )
    table = LLMSpend.__table__.c
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "day", "route"],
        set_={
            **{name: table[name] + stmt.excluded[name] for name in COUNTERS},
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


class SpendRecorder:
    """``LLMRouter.spend_sink``: each call in its own transaction, outside the caller's."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(self, entry: SpendEntry) -> None:
        async with self._session_factory() as session:
            await add(session, entry)
            await session.commit()


async def daily(session: AsyncSession, since: date) -> list[LLMSpend]:
    """Rows of ``since`` and later, by day, user and route."""
    result = await session.scalars(
        select(LLMSpend)
        .where(LLMSpend.day >= since)
        .order_by(LLMSpend.day, LLMSpend.user_id, LLMSpend.route)
    )
    return list(result.all())


async def spent_on(session: AsyncSession, user_id: uuid.UUID, day: date) -> Decimal:
    """The user's spend on ``day`` (UTC) over all routes; 0 when nothing was recorded."""
    total = await session.scalar(
        select(func.coalesce(func.sum(LLMSpend.cost_usd), 0)).where(
            LLMSpend.user_id == user_id, LLMSpend.day == day
        )
    )
    return Decimal(total or 0)


async def totals_on(session: AsyncSession, day: date) -> dict[uuid.UUID, Decimal]:
    """Every user's spend on ``day`` (UTC) with a recorded call, by user."""
    result = await session.execute(
        select(LLMSpend.user_id, func.sum(LLMSpend.cost_usd))
        .where(LLMSpend.day == day)
        .group_by(LLMSpend.user_id)
    )
    return {user_id: Decimal(total or 0) for user_id, total in result.all()}
