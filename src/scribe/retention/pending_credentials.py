"""Abandoned task-source connect flows lose their pending credentials (issue #56).

The Trello key/token step (``TasksStates``) and the Jira connect flow (``JiraStates``) keep the
credentials typed so far (the Trello key; the Jira email, API token or personal access token
waiting on the certificate card) in the encrypted ``bot_fsm_state.data`` until the flow ends.
The handlers check :data:`~scribe.telegram.control_bot.tasks_command.KEY_TTL` only on the user's
next input, so a flow the user walked away from would keep them indefinitely. This job deletes
such rows once they are untouched for ``KEY_TTL``; it runs every minute, so a pending secret
lives at most ``KEY_TTL`` plus one minute. The Trello token prompt (it echoes the key inside the
authorize link) is deleted from the chat too, best effort.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.clock import Clock, utcnow
from scribe.db.models import BotFsmState
from scribe.logging import get_logger
from scribe.scheduling.scheduler import Every, Scheduler
from scribe.telegram.control_bot.jira_connect import JiraStates
from scribe.telegram.control_bot.tasks_command import KEY_TTL, TasksStates

log = get_logger("scribe.retention")

JOB_NAME = "purge_pending_credentials"
JOB_SCHEDULE = Every(timedelta(minutes=1))
# aiogram stores "<GroupName>:<state>", e.g. "JiraStates:cert"
CONNECT_STATE_PREFIXES: tuple[str, ...] = (f"{TasksStates.__name__}:", f"{JiraStates.__name__}:")


async def purge_pending_credentials(
    session_factory: async_sessionmaker[AsyncSession], now: datetime, bot: Bot | None = None
) -> int:
    """Delete connect-flow FSM rows untouched for ``KEY_TTL``; return how many went."""
    async with session_factory() as session, session.begin():
        rows = (
            await session.execute(
                delete(BotFsmState)
                .where(
                    or_(
                        *(
                            BotFsmState.state.startswith(prefix, autoescape=True)
                            for prefix in CONNECT_STATE_PREFIXES
                        )
                    ),
                    BotFsmState.updated_at < now - KEY_TTL,
                )
                .returning(BotFsmState.data)
            )
        ).all()
    if bot is not None:
        for (data,) in rows:
            await _delete_prompt(bot, data or {})
    if rows:
        log.info("pending_credentials_purged", flows=len(rows))
    return len(rows)


async def _delete_prompt(bot: Bot, data: dict[str, Any]) -> None:
    prompt_id, chat_id = data.get("prompt_id"), data.get("chat_id")
    if not (isinstance(prompt_id, int) and isinstance(chat_id, int)):
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=prompt_id)
    except Exception as exc:  # gone already, or too old: nothing else to do
        log.info("credential_message_not_deleted", error=type(exc).__name__)


def register_pending_credentials_job(
    scheduler: Scheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bot: Bot | None = None,
    clock: Clock = utcnow,
) -> None:
    """Register ``purge_pending_credentials`` (every minute, due at once at start)."""

    async def job() -> None:
        await purge_pending_credentials(session_factory, clock(), bot)

    scheduler.register_job(JOB_NAME, JOB_SCHEDULE, job, run_at_start=True)
