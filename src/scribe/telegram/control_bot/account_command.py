"""``/my_data`` and ``/delete_my_data`` (FR-026, FR-030; contracts/control-chat.md).

- ``/my_data``: counts of what is stored about the caller by type, with the oldest item and when
  the retention job next removes something; **📦 Export** (``mydata:export``) sends a JSON file of
  every row of the caller (decrypted, grouped by table, no other user's data, never the Telegram
  session string or the task-source credentials) as a document in the control chat, and records
  ``activity_records(data_exported)`` with counts only. Both include the person's access
  (specs/005 T032): the summary an **Access** section (status, beta since, subscriptions, beta
  applications with status and dates, the count of access events), the export ``access.beta``,
  ``access.subscriptions``, ``access.events`` and ``access.applications`` (with the use case).
- ``/delete_my_data``: two steps. The command explains what is deleted (and, when the person has
  a beta entry or an active or scheduled subscription, that this access goes too: specs/005
  ``access.delete_warning``) and asks to type ``DELETE``; any other reply (or /cancel, or more
  than 10 minutes) cancels. Then: the Telegram session is logged out (:func:`log_out_telegram`),
  the user's client is stopped, the user row is deleted (every per-user table cascades, the FSM
  row by trigger, and the access rows keyed to the person's Telegram id go in the same
  transaction), the user's task-source cache entry is dropped, and a final message confirms.
  ``data_deleted`` goes to the logs only (the activity table is deleted with the user). If the
  deletion fails after the log-out, the Telegram session is marked ``revoked`` and the user is
  told the log-out already happened and to try again.

Payments (specs/015): ``/my_data`` lists the person's Stars subscription state and payments;
``/delete_my_data`` warns about a running Stars subscription (``pay.delete_warning_running``) and
about the minimal payment record that is kept for 3 years (``pay.delete_warning_record``), and
on confirmation first cancels the renewal on Telegram (:func:`cancel_stars_before_delete`). If
Telegram refuses, nothing is deleted (``pay.delete_cancel_failed``) and the person can retry, so
nobody is charged after deleting.

The summary also shows the grammatical gender for drafts (issue #46; the export has it in the
user row). Neither the summary nor the export is logged.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scribe.access import policy
from scribe.access import repo as access_repo
from scribe.clock import Clock, utcnow
from scribe.config import Settings
from scribe.context.tasks.service import default_task_sources
from scribe.db.models import ActivityKind, SessionStatus, StarsCancelSource, StarsSubState, User
from scribe.db.repos import activity as activity_repo
from scribe.db.repos import users as users_repo
from scribe.i18n import Lang, format_day_month_year, t
from scribe.logging import get_logger
from scribe.payments import lifecycle
from scribe.payments import repo as payments_repo
from scribe.payments.offers import price_text
from scribe.payments.telegram import StarsApi, StarsApiError
from scribe.retention.user_data import (
    AccessSummary,
    DataSummary,
    TableSummary,
    data_summary,
    delete_user_data,
    export_user_data,
)
from scribe.telegram.control_bot.grammatical_gender import gender_label
from scribe.telegram.control_bot.render import button, format_callback, keyboard
from scribe.telegram.gateway import MessagingGateway

log = get_logger("scribe.control_bot")

CONFIRM_WORD = "DELETE"
CONFIRM_TTL = timedelta(minutes=10)


class DeleteStates(StatesGroup):
    confirm = State()


# --- /my_data -----------------------------------------------------------------------------------


def _day(moment: datetime | None, tz: str, lang: str) -> str:
    return format_day_month_year(moment.astimezone(ZoneInfo(tz)), lang) if moment else "—"


def _work_account_line(summary: DataSummary, tz: str, lang: str) -> str:
    counts = {"auto": summary.auto_chats, "excluded": summary.excluded_chats}
    if summary.work_account:
        day = _day(summary.work_account_since, tz, lang)
        return t("my_data.work_account_on", lang, day=day, **counts)
    return t("my_data.work_account_off", lang, **counts)


def _line(label: str, summary: TableSummary, tz: str, rule: str, lang: str) -> str:
    details = []
    if summary.oldest is not None:
        details.append(t("my_data.oldest", lang, day=_day(summary.oldest, tz, lang)))
    details.append(rule)
    if summary.next_removal is not None:
        details.append(t("my_data.next_removal", lang, day=_day(summary.next_removal, tz, lang)))
    return t(label, lang, n=summary.count, details="; ".join(details))


def access_lines(access: AccessSummary, tz: str, lang: str) -> list[str]:
    """The **Access** section of ``/my_data`` (specs/005 contracts/control-chat.md)."""
    status = t(f"access.mydata_status_{access.kind}", lang)
    lines = [t("access.mydata_title", lang, status=status)]
    if access.beta_since is not None:
        lines.append(t("access.mydata_beta_since", lang, day=_day(access.beta_since, tz, lang)))
    for sub in access.subscriptions:
        lines.append(
            t(
                "access.mydata_subscription",
                lang,
                start=_day(sub.starts_at, tz, lang),
                end=_day(sub.ends_at, tz, lang),
                state=t(f"access.mydata_sub_{sub.state}", lang),
            )
        )
    for app in access.applications:
        status = t(f"access.mydata_app_{app.status}", lang, day=_day(app.decided_at, tz, lang))
        lines.append(
            t(
                "access.mydata_application",
                lang,
                day=_day(app.submitted_at, tz, lang),
                status=status,
            )
        )
    lines += payment_lines(access, tz, lang)  # specs/015: empty without Stars payments
    lines.append(t("access.mydata_events", lang, n=access.events))
    return lines


def payment_lines(access: AccessSummary, tz: str, lang: str) -> list[str]:
    """The Stars subscription state and the payments of ``/my_data`` (specs/015 FR-019)."""
    lines: list[str] = []
    stars = access.stars
    if stars is not None:
        if stars.state == "active":
            state = t(
                "pay.mydata_stars_active",
                lang,
                date=_day(stars.period_end, tz, lang),
                price=price_text(stars.price),
            )
        elif stars.state == "canceled":
            state = t("pay.mydata_stars_canceled", lang, date=_day(stars.period_end, tz, lang))
        else:
            state = t(
                "pay.mydata_stars_ended",
                lang,
                date=_day(stars.ended_at or stars.period_end, tz, lang),
            )
        lines.append(t("pay.mydata_stars", lang, state=state))
    for payment in access.payments:
        refunded = (
            t("pay.mydata_refunded", lang, date=_day(payment.refunded_at, tz, lang))
            if payment.refunded_at is not None
            else ""
        )
        lines.append(
            t(
                "pay.mydata_payment",
                lang,
                date=_day(payment.paid_at, tz, lang),
                price=price_text(payment.amount),
                kind=t(f"pay.mydata_kind_{payment.kind}", lang),
                refunded=refunded,
            )
        )
    return lines


def summary_text(summary: DataSummary, tz: str, retention_days: int, lang: str = "en") -> str:
    states = summary.profiles_by_state
    commitments = t(
        "my_data.commitments", lang, open=summary.commitments_open, closed=summary.commitments.count
    )
    if summary.commitments.next_removal:
        commitments += t(
            "my_data.commitments_removal",
            lang,
            day=_day(summary.commitments.next_removal, tz, lang),
        )
    not_connected = t("my_data.not_connected", lang)
    lines = [
        t("my_data.title", lang),
        _line(
            "my_data.messages", summary.messages, tz,
            t("my_data.messages_rule", lang, n=retention_days), lang,
        ),
        _line("my_data.drafts", summary.drafts, tz, t("my_data.drafts_rule", lang), lang),
        _line(
            "my_data.profiles", summary.profiles, tz,
            t(
                "my_data.profiles_rule", lang,
                current=states.get("current", 0),
                previous=states.get("previous", 0),
                retired=states.get("retired", 0),
            ),
            lang,
        ),
        t("my_data.proposals", lang, n=summary.proposals),
        commitments,
        _line("my_data.activity", summary.activity, tz, t("my_data.activity_rule", lang), lang),
        _line("my_data.decisions", summary.decisions, tz, t("my_data.decisions_rule", lang), lang),
        t("my_data.release_notes", lang, n=summary.release_notes),
        t("settings.gender", lang, value=gender_label(summary.grammatical_gender, lang)),
        t("my_data.chats", lang, contacts=summary.contacts, work_chats=summary.work_chats),
        _work_account_line(summary, tz, lang),  # specs/008
        t(
            "my_data.connections", lang,
            telegram=summary.telegram_status or not_connected,
            tasks=summary.task_source or not_connected,
        ),
    ]  # fmt: skip
    if summary.access is not None:
        lines += ["", *access_lines(summary.access, tz, lang)]
    lines += ["", t("my_data.footer", lang)]
    return "\n".join(lines)


async def my_data_command(
    message: Message,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    async with session_factory() as session:
        summary = await data_summary(
            session,
            user.id,
            message_retention_days=settings.message_retention_days,
            now=clock(),
            is_operator=policy.is_operator(user.telegram_control_chat_id, settings),
        )
    await message.answer(
        summary_text(summary, user.timezone, settings.message_retention_days, lang),
        reply_markup=keyboard(
            [[button(t("btn.export", lang), format_callback("mydata", "export"))]]
        ),
    )


async def export_callback(
    callback: CallbackQuery,
    bot: Bot,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await callback.answer(t("common.not_set_up", lang))
        return
    await callback.answer(t("alert.export_preparing", lang))
    now = clock()
    async with session_factory() as session:
        export = await export_user_data(session, user.id, now=now)
        counts = {table: len(rows) for table, rows in export.items() if isinstance(rows, list)}
        await activity_repo.record(
            session, user.id, ActivityKind.data_exported, at=now, meta={"rows": counts}
        )
        await session.commit()
    payload = json.dumps(export, ensure_ascii=False, indent=2).encode("utf-8")
    await bot.send_document(
        user.telegram_control_chat_id,
        BufferedInputFile(payload, filename=f"scribe-data-{now:%Y-%m-%d}.json"),
        caption=t("my_data.export_caption", lang),
    )
    log.info("data_exported", user_id=str(user.id), tables=len(counts), bytes=len(payload))


# --- /delete_my_data ----------------------------------------------------------------------------


async def log_out_telegram(gateway: MessagingGateway, user: User) -> None:
    """End the user's Telegram session (FR-030) with ``MessagingGateway.log_out``
    (``ClientPool.log_out``: auth.logOut, also without a running client).

    A gateway without ``log_out`` (a test double) only logs a warning; the session string is
    then removed with the user row, and the session stays listed in Telegram's Active sessions
    until the user ends it there.
    """
    log_out: Callable[[Any], Awaitable[None]] | None = getattr(gateway, "log_out", None)
    if log_out is None:
        log.warning("telegram_log_out_unavailable", user_id=str(user.id))
        return
    await log_out(user.id)


async def access_warning(
    session_factory: async_sessionmaker[AsyncSession], user: User, now: datetime, lang: str
) -> str | None:
    """``access.delete_warning`` when deleting would also remove a beta entry or an active or
    scheduled subscription (specs/005 research Q10); ``None`` when there is nothing to lose."""
    async with session_factory() as session:
        telegram_id = user.telegram_control_chat_id
        beta = await access_repo.find_beta(session, telegram_id, None, now=now)
        subs = await access_repo.subscriptions_for(session, telegram_id)
        running = await payments_repo.running_for(session, telegram_id)
        paid = [
            p for p in await payments_repo.payments_for(session, telegram_id)
            if p.owner_deleted_at is None
        ]  # fmt: skip
    ends = [s.ends_at for s in subs if s.cancelled_at is None and s.ends_at > now]
    parts: list[str] = []
    if beta is not None or ends:
        if not ends:
            what = t("access.delete_what_beta", lang)
        else:
            day = _day(max(ends), user.timezone, lang)
            key = (
                "access.delete_what_both" if beta is not None else "access.delete_what_subscription"
            )
            what = t(key, lang, date=day)
        parts.append(t("access.delete_warning", lang, what=what))
    if running is not None:  # specs/015: the renewal is cancelled, paid days are lost
        parts.append(t("pay.delete_warning_running", lang))
    if paid:  # specs/015: the minimal payment record outlives the deletion
        parts.append(t("pay.delete_warning_record", lang))
    return "\n\n".join(parts) if parts else None


async def cancel_stars_before_delete(
    session_factory: async_sessionmaker[AsyncSession],
    stars_api: StarsApi | None,
    telegram_id: int,
    now: datetime,
) -> bool:
    """Cancel an ``active`` Stars subscription on Telegram before anything is deleted (specs/015
    research P10). False when Telegram refuses (or no connection exists): nothing was changed
    and the deletion must stop."""
    async with session_factory() as session:
        running = await payments_repo.running_for(session, telegram_id)
        if running is None or running.state is not StarsSubState.active:
            return True  # nothing is going to be charged
        try:
            if stars_api is None:
                raise StarsApiError("payments are not wired")
            await lifecycle.cancel_renewal(
                session, stars_api, telegram_id=telegram_id, by=StarsCancelSource.deletion, now=now
            )
        except StarsApiError as exc:
            await session.rollback()
            log.error(
                "stars_subscription_state",
                result="error",
                action="cancel",
                by="deletion",
                error=str(exc),
            )
            return False
        await session.commit()
        return True


async def delete_command(
    message: Message,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    lang: Lang,
    clock: Clock = utcnow,
) -> None:
    if user is None:
        await message.answer(t("common.not_set_up", lang))
        return
    now = clock()
    warning = await access_warning(session_factory, user, now, lang)
    await state.set_state(DeleteStates.confirm)
    await state.set_data({"asked_at": now.isoformat()})
    ask = t("delete.ask", lang, word=CONFIRM_WORD)
    await message.answer(f"{ask}\n\n{warning}" if warning else ask)


async def delete_confirm_input(
    message: Message,
    bot: Bot,
    state: FSMContext,
    user: User | None,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: MessagingGateway,
    lang: Lang,
    clock: Clock = utcnow,
    stars_api: StarsApi | None = None,
) -> None:
    data = await state.get_data()
    if user is None:
        await state.clear()
        return
    try:
        asked_at = datetime.fromisoformat(str(data.get("asked_at")))
    except ValueError:
        asked_at = None
    fresh = asked_at is not None and clock() - asked_at <= CONFIRM_TTL
    if (message.text or "").strip() != CONFIRM_WORD or not fresh:
        await state.clear()
        await message.answer(t("delete.cancelled", lang))
        return
    chat_id = user.telegram_control_chat_id
    # Specs/015: stop the Stars renewal first; if Telegram refuses, nothing is deleted.
    if not await cancel_stars_before_delete(session_factory, stars_api, chat_id, clock()):
        await state.clear()
        await bot.send_message(chat_id, t("pay.delete_cancel_failed", lang))
        return
    try:
        await log_out_telegram(gateway, user)
    except Exception as exc:
        log.error("telegram_log_out_failed", user_id=str(user.id), error=type(exc).__name__)
        await state.clear()
        await bot.send_message(chat_id, t("delete.logout_failed", lang))
        return
    try:
        await gateway.stop(user.id)
    except Exception as exc:
        log.error("telegram_client_stop_failed", user_id=str(user.id), error=type(exc).__name__)
    await state.clear()
    try:
        async with session_factory() as session:
            await delete_user_data(session, user.id, now=clock())
            await session.commit()
    except Exception as exc:
        # The session is logged out already: record that, so nothing tries to use it, and say
        # so. Nothing else changed; a retry deletes the rest.
        log.error("data_delete_failed", user_id=str(user.id), error=type(exc).__name__)
        await _mark_logged_out(session_factory, user)
        await bot.send_message(chat_id, t("delete.failed", lang))
        return
    # To the logs only: the activity table went with the user.
    log.info("data_deleted", user_id=str(user.id), at=clock().isoformat())
    default_task_sources().invalidate(user.id)
    await bot.send_message(chat_id, t("delete.done", lang))


async def _mark_logged_out(session_factory: async_sessionmaker[AsyncSession], user: User) -> None:
    try:
        async with session_factory() as session:
            await users_repo.set_telegram_session_status(session, user.id, SessionStatus.revoked)
            await session.commit()
    except Exception as exc:
        log.error("telegram_session_mark_failed", user_id=str(user.id), error=type(exc).__name__)


async def delete_cancel(message: Message, state: FSMContext, lang: Lang) -> None:
    await state.clear()
    await message.answer(t("delete.cancelled", lang))


class AbandonedDeleteMiddleware(BaseMiddleware):
    """Any other command during the DELETE confirmation cancels it, so a later "DELETE" typed
    for another reason never deletes the account. Outer middleware on the settings router."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        text = event.text if isinstance(event, Message) else None
        if (
            isinstance(state, FSMContext)
            and text is not None
            and text.startswith("/")
            and not text.startswith(("/cancel", "/delete_my_data"))
            and await state.get_state() == DeleteStates.confirm.state
        ):
            await state.clear()
        return await handler(event, data)


def create_router(idle: StateFilter) -> Router:
    """``idle``: the states in which control-chat commands answer (see ``settings.py``)."""
    router = Router(name="account")
    router.message.register(my_data_command, Command("my_data"), idle)
    router.message.register(delete_command, Command("delete_my_data"), idle)
    router.message.register(delete_cancel, Command("cancel"), StateFilter(DeleteStates))
    router.message.register(
        delete_confirm_input, StateFilter(DeleteStates.confirm), F.text & ~F.text.startswith("/")
    )
    router.callback_query.register(export_callback, F.data == "mydata:export", idle)
    return router


__all__ = [
    "AbandonedDeleteMiddleware",
    "DeleteStates",
    "create_router",
    "log_out_telegram",
    "summary_text",
]
