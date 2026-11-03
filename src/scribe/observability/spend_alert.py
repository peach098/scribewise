"""The operators' daily LLM spend alert (issue #74).

Every day at 00:20 UTC the ``llm_spend_alert`` job reads every user's total of the UTC day that
just ended (``llm_spend``) and sends each operator (``SCRIBE_OPERATOR_TELEGRAM_IDS``) one
message, in the operator's stored language, listing by ``users.id``:

- users above the 95th percentile of that day's spend (nearest-rank percentile over the users
  with any spend; only from :data:`PERCENTILE_MIN_USERS` such users on, below that one or two
  heavy users would make every percentile meaningless, so the alert says it was skipped);
- users who reached the hard cap (drafting was paused) and, separately, those who reached only
  the soft cap (drafts on the economy tier), by the caps configured now.

User ids and amounts only: no names, chats or message content. Nothing is sent when no section
has anyone in it, or when no operator is configured.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import Decimal

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access.apply import operator_languages
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.db.repos import llm_spend as llm_spend_repo
from scribe.drafting.budget import BudgetCaps, utc_day
from scribe.i18n import DEFAULT_LANG, t
from scribe.logging import get_logger
from scribe.scheduling.scheduler import Daily, Scheduler

log = get_logger("scribe.spend_alert")

JOB_NAME = "llm_spend_alert"
JOB_SCHEDULE = Daily(at=time(0, 20))  # UTC: the day being reported has just ended
PERCENTILE = 0.95
PERCENTILE_MIN_USERS = 20


@dataclass(frozen=True)
class SpendAlert:
    day: date
    users: int
    total: Decimal
    p95: Decimal | None  # None: too few users for a percentile
    above_p95: list[tuple[uuid.UUID, Decimal]]
    soft: list[tuple[uuid.UUID, Decimal]]  # reached the soft cap but not the hard one
    hard: list[tuple[uuid.UUID, Decimal]]

    @property
    def empty(self) -> bool:
        return not (self.above_p95 or self.soft or self.hard)


def nearest_rank(values: list[Decimal], fraction: float) -> Decimal:
    """The nearest-rank percentile: the smallest value with at least ``fraction`` of the values
    at or below it."""
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def build_alert(day: date, totals: Mapping[uuid.UUID, Decimal], caps: BudgetCaps) -> SpendAlert:
    spenders = {user_id: total for user_id, total in totals.items() if total > 0}

    def by_amount(ids: list[uuid.UUID]) -> list[tuple[uuid.UUID, Decimal]]:
        return sorted(((u, spenders[u]) for u in ids), key=lambda item: (-item[1], str(item[0])))

    p95 = None
    above: list[uuid.UUID] = []
    if len(spenders) >= PERCENTILE_MIN_USERS:
        p95 = nearest_rank(list(spenders.values()), PERCENTILE)
        above = [u for u, total in spenders.items() if total > p95]
    levels = {u: caps.level(total) for u, total in spenders.items()}
    return SpendAlert(
        day=day,
        users=len(spenders),
        total=sum(spenders.values(), Decimal(0)),
        p95=p95,
        above_p95=by_amount(above),
        soft=by_amount([u for u, level in levels.items() if level == "soft"]),
        hard=by_amount([u for u, level in levels.items() if level == "hard"]),
    )


def _usd(value: Decimal | float) -> str:
    return f"${Decimal(str(value)):.2f}"


def render_alert(alert: SpendAlert, caps: BudgetCaps, lang: str) -> str:
    lines = [
        t("spend_alert.header", lang, day=alert.day.isoformat(), n=alert.users,
          total=_usd(alert.total))
    ]  # fmt: skip

    def section(title: str, rows: list[tuple[uuid.UUID, Decimal]]) -> None:
        if rows:
            lines.extend(["", title, *(f"• {user_id} — {_usd(total)}" for user_id, total in rows)])

    if alert.p95 is None:
        lines.append(t("spend_alert.p95_skipped", lang, min=PERCENTILE_MIN_USERS))
    else:
        section(t("spend_alert.p95", lang, p95=_usd(alert.p95)), alert.above_p95)
    section(t("spend_alert.hard", lang, cap=_usd(caps.hard_usd)), alert.hard)
    section(t("spend_alert.soft", lang, cap=_usd(caps.soft_usd)), alert.soft)
    return "\n".join(lines)


async def send_spend_alert(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    *,
    clock: Clock = utcnow,
) -> int:
    """Report yesterday (UTC) to every operator; how many messages were sent."""
    operators = settings.operator_telegram_ids
    if not operators:
        return 0
    day = utc_day(clock()) - timedelta(days=1)
    caps = BudgetCaps.from_settings(settings)
    async with session_factory() as session:
        totals = await llm_spend_repo.totals_on(session, day)
    alert = build_alert(day, totals, caps)
    if alert.empty:
        log.info("spend_alert_nothing_to_report", day=day.isoformat(), users=alert.users)
        return 0
    langs = await operator_languages(session_factory, operators)
    sent = 0
    for chat_id in operators:
        try:
            await bot.send_message(
                chat_id,
                render_alert(alert, caps, langs.get(chat_id, DEFAULT_LANG)),
                parse_mode=None,
            )
        except Exception as exc:  # blocked bot, network: the other operators still get it
            log.warning("spend_alert_send_failed", error=type(exc).__name__)
            continue
        sent += 1
    log.info(
        "spend_alert_sent",
        day=day.isoformat(),
        users=alert.users,
        above_p95=len(alert.above_p95),
        soft=len(alert.soft),
        hard=len(alert.hard),
        operators=sent,
    )
    return sent


def register_spend_alert_job(
    scheduler: Scheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    clock: Clock = utcnow,
) -> None:
    async def job() -> None:
        await send_spend_alert(session_factory, bot, settings, clock=clock)

    scheduler.register_job(JOB_NAME, JOB_SCHEDULE, job)


__all__ = [
    "JOB_NAME",
    "PERCENTILE_MIN_USERS",
    "SpendAlert",
    "build_alert",
    "nearest_rank",
    "register_spend_alert_job",
    "render_alert",
    "send_spend_alert",
]
